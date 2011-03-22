from pypy.interpreter import gateway
from pypy.interpreter.error import OperationError
from pypy.interpreter.pyparser import parser, pytokenizer, pygram, error
from pypy.interpreter.astcompiler import consts
from pypy.interpreter.unicodehelper import PyUnicode_EncodeUTF8



_recode_to_utf8 = gateway.applevel(r'''
    def _recode_to_utf8(text, encoding):
        return unicode(text, encoding).encode("utf-8")
''').interphook('_recode_to_utf8')

def recode_to_utf8(space, text, encoding):
    return space.str_w(_recode_to_utf8(space, space.wrap(text),
                                          space.wrap(encoding)))

def _normalize_encoding(encoding):
    """returns normalized name for <encoding>

    see dist/src/Parser/tokenizer.c 'get_normal_name()'
    for implementation details / reference

    NOTE: for now, parser.suite() raises a MemoryError when
          a bad encoding is used. (SF bug #979739)
    """
    if encoding is None:
        return None
    # lower() + '_' / '-' conversion
    encoding = encoding.replace('_', '-').lower()
    if encoding == 'utf-8' or encoding.startswith('utf-8-'):
        return 'utf-8'
    for variant in ['latin-1', 'iso-latin-1', 'iso-8859-1']:
        if (encoding == variant or
            encoding.startswith(variant + '-')):
            return 'iso-8859-1'
    return encoding

def _check_for_encoding(s):
    eol = s.find('\n')
    if eol < 0:
        enc = _check_line_for_encoding(s)
    else:
        enc = _check_line_for_encoding(s[:eol])
    if enc:
        return enc
    if eol >= 0:
        eol2 = s.find('\n', eol + 1)
        if eol2 < 0:
            return _check_line_for_encoding(s[eol + 1:])
        return _check_line_for_encoding(s[eol + 1:eol2])


def _check_line_for_encoding(line):
    """returns the declared encoding or None"""
    i = 0
    for i in range(len(line)):
        if line[i] == '#':
            break
        if line[i] not in ' \t\014':
            return None
    return pytokenizer.match_encoding_declaration(line[i:])


class CompileInfo(object):
    """Stores information about the source being compiled.

    * filename: The filename of the source.
    * mode: The parse mode to use. ('exec', 'eval', or 'single')
    * flags: Parser and compiler flags.
    * encoding: The source encoding.
    * last_future_import: The line number and offset of the last __future__
      import.
    * hidden_applevel: Will this code unit and sub units be hidden at the
      applevel?
    """

    def __init__(self, filename, mode="exec", flags=0, future_pos=(0, 0),
                 hidden_applevel=False):
        self.filename = filename
        self.mode = mode
        self.encoding = None
        self.flags = flags
        self.last_future_import = future_pos
        self.hidden_applevel = hidden_applevel


_targets = {
'eval' : pygram.syms.eval_input,
'single' : pygram.syms.single_input,
'exec' : pygram.syms.file_input,
}

class Stream(object):
    "Pseudo-file object used by PythonParser.parse_file"

    def readline(self):
        raise NotImplementedError

    encoding = None
    def set_encoding(self, encoding):
        self.encoding = encoding

    def close(self):
        pass


class StdStream(Stream):
    def __init__(self, space, stream):
        self.space = space
        self.stream = stream
        self.w_readline = None
        self.w_file = None

    def readline(self):
        if not self.w_readline:
            return self.stream.readline()
        else:
            w_line = self.space.call_function(self.w_readline)
            return PyUnicode_EncodeUTF8(self.space,
                                        self.space.unicode_w(w_line))

    def set_encoding(self, encoding):
        self.encoding = encoding
        self.w_readline = None
        if encoding:
            from pypy.module._codecs.interp_codecs import lookup_codec
            from pypy.module._file import interp_file
            space = self.space
            w_codec_tuple = lookup_codec(space, encoding)
            self.w_file = interp_file.from_stream(space, self.stream, 'r')
            w_stream_reader = space.getitem(w_codec_tuple, space.wrap(2))
            w_reader = space.call_function(w_stream_reader, self.w_file)
            self.w_readline = space.getattr(w_reader, space.wrap('readline'))

    def close(self):
        if self.w_file:
            from pypy.module._file import interp_file
            interp_file.detach_stream(self.space, self.w_file)

class PythonParser(parser.Parser):

    IMPORT_FROM = pygram.python_grammar.symbol_ids['import_from']

    def __init__(self, space):
        parser.Parser.__init__(self, pygram.python_grammar)
        self.space = space

    def _detect_encoding(self, text, lineno, compile_info):
        "Detect source encoding from the beginning of the file"
        if lineno == 1 and text.startswith("\xEF\xBB\xBF"):
            text = text[3:]
            compile_info.encoding = 'utf-8'
            # If an encoding is explicitly given check that it is utf-8.
            decl_enc = _check_for_encoding(text)
            if decl_enc and decl_enc != "utf-8":
                raise error.SyntaxError("UTF-8 BOM with non-utf8 coding cookie",
                                        filename=compile_info.filename)
        elif compile_info.flags & consts.PyCF_SOURCE_IS_UTF8:
            compile_info.encoding = 'utf-8'
            if _check_for_encoding(text) is not None:
                raise error.SyntaxError("coding declaration in unicode string",
                                        filename=compile_info.filename)
        else:
            compile_info.encoding = _normalize_encoding(
                _check_for_encoding(text))
        return text

    def _decode_error(self, e, compile_info):
        space = self.space
        # if the codec is not found, LookupError is raised.  we
        # check using 'is_w' not to mask potential IndexError or
        # KeyError
        if space.is_w(e.w_type, space.w_LookupError):
            return error.SyntaxError(
                "Unknown encoding: %s" % compile_info.encoding,
                filename=compile_info.filename)
        # Transform unicode errors into SyntaxError
        if e.match(space, space.w_UnicodeDecodeError):
            e.normalize_exception(space)
            w_message = space.str(e.get_w_value(space))
            return error.SyntaxError(space.str_w(w_message))

    def parse_source(self, textsrc, compile_info):
        """Main entry point for parsing Python source.

        Everything from decoding the source to tokenizing to building the parse
        tree is handled here.
        """
        textsrc = self._detect_encoding(textsrc, 1, compile_info)

        enc = compile_info.encoding
        if enc is not None and enc not in ('utf-8', 'iso-8859-1'):
            try:
                textsrc = recode_to_utf8(self.space, textsrc, enc)
            except OperationError, e:
                operror = self._decode_error(e, compile_info)
                if operror:
                    raise operror
                else:
                    raise

        source_lines = textsrc.splitlines(True)

        return self.build_tree(source_lines, compile_info)

    def parse_file(self, stream, compile_info):
        assert isinstance(stream, Stream)

        source_lines = []

        while len(source_lines) < 2:
            line = stream.readline()
            if not line:
                break
            line = self._detect_encoding(
                line, 1, compile_info)
            source_lines.append(line)
            if compile_info.encoding is not None:
                break

        enc = compile_info.encoding
        if enc in ('utf-8', 'iso-8859-1'):
            enc = None # No need to recode
        stream.set_encoding(enc)

        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                source_lines.append(line)
        except OperationError, e:
            operror = self._decode_error(e, compile_info)
            if operror:
                raise operror
            else:
                raise

        return self.build_tree(source_lines, compile_info)

    def parse_future_import(self, node):
        if node.type != self.IMPORT_FROM:
            return
        children = node.children
        # from __future__ import ..., must have at least 4 children
        if len(children) < 4:
            return
        if children[0].value != 'from':
            return
        if not children[1].children or len(children[1].children) != 1:
            return
        if children[1].children[0].value != '__future__':
            return

        child = children[3]
        # child can be a star, a parenthesis or import_as_names
        if child.type == pygram.tokens.STAR:
            return
        if child.type == pygram.tokens.LPAR:
            child = children[4]

        for i in range(0, len(child.children), 2):
            c = child.children[i]
            if (len(c.children) == 0 or
                c.children[0].type != pygram.tokens.NAME):
                continue

            name = c.children[0].value

            if name == 'print_function':
                self.compile_info.flags |= consts.CO_FUTURE_PRINT_FUNCTION
            elif name == 'with_statement':
                self.compile_info.flags |= consts.CO_FUTURE_WITH_STATEMENT
            elif name == 'unicode_literals':
                self.compile_info.flags |= consts.CO_FUTURE_UNICODE_LITERALS

    def classify(self, token_type, value, *args):
        if self.compile_info.flags & consts.CO_FUTURE_PRINT_FUNCTION:
            if token_type == self.grammar.KEYWORD_TOKEN and value == 'print':
                return self.grammar.token_ids[pygram.tokens.NAME]
        return parser.Parser.classify(self, token_type, value, *args)

    def pop(self):
        node = parser.Parser.pop(self)
        self.parse_future_import(node)
        return node

    def build_tree(self, source_lines, compile_info):
        """Builds the parse tree from a list of source lines"""

        if source_lines and source_lines[-1]:
            last_line = source_lines[-1]
            if last_line:
                if last_line[-1] == "\n":
                    compile_info.flags &= ~consts.PyCF_DONT_IMPLY_DEDENT
                else:
                    # The tokenizer is very picky about how it wants its input.
                    source_lines[-1] += '\n'

        self.prepare(_targets[compile_info.mode])
        self.compile_info = compile_info
        tp = 0
        try:
            try:
                tokens = pytokenizer.generate_tokens(source_lines,
                                                     compile_info.flags)
                for tp, value, lineno, column, line in tokens:
                    if self.add_token(tp, value, lineno, column, line):
                        break
            except error.TokenError, e:
                e.filename = compile_info.filename
                raise
            except parser.ParseError, e:
                # Catch parse errors, pretty them up and reraise them as a
                # SyntaxError.
                new_err = error.IndentationError
                if tp == pygram.tokens.INDENT:
                    msg = "unexpected indent"
                elif e.expected == pygram.tokens.INDENT:
                    msg = "expected an indented block"
                else:
                    new_err = error.SyntaxError
                    msg = "invalid syntax"
                raise new_err(msg, e.lineno, e.column, e.line,
                              compile_info.filename)
            else:
                tree = self.root
        finally:
            # Avoid hanging onto the tree.
            self.root = None
            self.compile_info = None
        return tree
