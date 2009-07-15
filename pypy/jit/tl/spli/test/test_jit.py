
import py
from pypy.jit.metainterp.test.test_basic import JitMixin
from pypy.jit.tl.spli import interpreter, objects, serializer
from pypy.jit.metainterp.typesystem import LLTypeHelper, OOTypeHelper
from pypy.jit.backend.llgraph import runner
from pypy.rpython.annlowlevel import llstr, hlstr

class TestSPLIJit(JitMixin):
    type_system = 'lltype'
    CPUClass = runner.LLtypeCPU
    ts = LLTypeHelper()
    
    def interpret(self, f, args):
        coderepr = serializer.serialize(f.func_code)
        arg_params = ", ".join(['arg%d' % i for i in range(len(args))])
        arg_ass = ";".join(['frame.locals[%d] = space.wrap(arg%d)' % (i, i) for
                                 i in range(len(args))])
        space = objects.DumbObjSpace()
        source = py.code.Source("""
        def bootstrap(%(arg_params)s):
            co = serializer.deserialize(coderepr)
            frame = interpreter.SPLIFrame(co)
            %(arg_ass)s
            return frame.run()
        """ % locals())
        d = globals().copy()
        d['coderepr'] = coderepr
        d['space'] = space
        exec source.compile() in d
        return self.meta_interp(d['bootstrap'], args, listops=True)
    
    def test_basic(self):
        def f():
            i = 0
            while i < 20:
                i = i + 1
            return i
        self.interpret(f, [])

    def test_bridge(self):
        def f(a, b):
            total = 0
            i = 0
            while i < 100:
                if i & 1:
                    total = total + a
                else:
                    total = total + b
                i = i + 1
            return total

        self.interpret(f, [1, 10])
