from pypy.interpreter.error import OperationError, wrap_oserror
from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.typedef import TypeDef, GetSetProperty
from pypy.interpreter.gateway import interp2app, unwrap_spec
from rpython.rlib import rmmap, rarithmetic
from rpython.rlib.buffer import Buffer
from rpython.rlib.rmmap import RValueError, RTypeError, RMMapError
from rpython.rlib.rstring import StringBuilder

if rmmap.HAVE_LARGEFILE_SUPPORT:
    OFF_T = rarithmetic.r_longlong
else:
    OFF_T = int


class W_MMap(W_Root):
    def __init__(self, space, mmap_obj):
        self.space = space
        self.mmap = mmap_obj

    def buffer_w(self, space, flags):
        self.check_valid()
        return MMapBuffer(self.space, self.mmap,
                          bool(flags & space.BUF_WRITABLE))

    def close(self):
        self.mmap.close()

    def read_byte(self):
        self.check_valid()
        try:
            return self.space.wrap(ord(self.mmap.read_byte()))
        except RValueError, v:
            raise mmap_error(self.space, v)

    def readline(self):
        self.check_valid()
        return self.space.wrapbytes(self.mmap.readline())

    @unwrap_spec(num=int)
    def read(self, num=-1):
        self.check_valid()
        return self.space.wrapbytes(self.mmap.read(num))

    def find(self, w_tofind, w_start=None, w_end=None):
        self.check_valid()
        space = self.space
        tofind = space.getarg_w('s#', w_tofind)
        if w_start is None:
            start = self.mmap.pos
        else:
            start = space.getindex_w(w_start, None)
        if w_end is None:
            end = self.mmap.size
        else:
            end = space.getindex_w(w_end, None)
        return space.wrap(self.mmap.find(tofind, start, end))

    def rfind(self, w_tofind, w_start=None, w_end=None):
        self.check_valid()
        space = self.space
        tofind = space.getarg_w('s#', w_tofind)
        if w_start is None:
            start = self.mmap.pos
        else:
            start = space.getindex_w(w_start, None)
        if w_end is None:
            end = self.mmap.size
        else:
            end = space.getindex_w(w_end, None)
        return space.wrap(self.mmap.find(tofind, start, end, True))

    @unwrap_spec(pos=OFF_T, whence=int)
    def seek(self, pos, whence=0):
        self.check_valid()
        try:
            self.mmap.seek(pos, whence)
        except RValueError, v:
            raise mmap_error(self.space, v)

    def tell(self):
        self.check_valid()
        return self.space.wrap(self.mmap.tell())

    def descr_size(self):
        self.check_valid()
        try:
            return self.space.wrap(self.mmap.file_size())
        except OSError, e:
            raise mmap_error(self.space, e)

    def write(self, w_data):
        self.check_valid()
        data = self.space.getarg_w('s#', w_data)
        self.check_writeable()
        try:
            self.mmap.write(data)
        except RValueError, v:
            raise mmap_error(self.space, v)

    @unwrap_spec(byte=int)
    def write_byte(self, byte):
        self.check_valid()
        self.check_writeable()
        try:
            self.mmap.write_byte(chr(byte))
        except RMMapError, v:
            raise mmap_error(self.space, v)

    @unwrap_spec(offset=int, size=int)
    def flush(self, offset=0, size=0):
        self.check_valid()
        try:
            return self.space.wrap(self.mmap.flush(offset, size))
        except RValueError, v:
            raise mmap_error(self.space, v)
        except OSError, e:
            raise mmap_error(self.space, e)

    @unwrap_spec(dest=int, src=int, count=int)
    def move(self, dest, src, count):
        self.check_valid()
        self.check_writeable()
        try:
            self.mmap.move(dest, src, count)
        except RValueError, v:
            raise mmap_error(self.space, v)

    @unwrap_spec(newsize=int)
    def resize(self, newsize):
        self.check_valid()
        self.check_resizeable()
        try:
            self.mmap.resize(newsize)
        except OSError, e:
            raise mmap_error(self.space, e)
        except RValueError, e:
            # obscure: in this case, RValueError translates to an app-level
            # SystemError.
            raise OperationError(self.space.w_SystemError,
                                 self.space.wrap(e.message))

    def __len__(self):
        return self.space.wrap(self.mmap.size)

    def closed_get(self, space):
        try:
            self.mmap.check_valid()
        except RValueError:
            return space.w_True
        return space.w_False

    def check_valid(self):
        try:
            self.mmap.check_valid()
        except RValueError, v:
            raise mmap_error(self.space, v)

    def check_writeable(self):
        try:
            self.mmap.check_writeable()
        except RMMapError, v:
            raise mmap_error(self.space, v)

    def check_resizeable(self):
        try:
            self.mmap.check_resizeable()
        except RMMapError, v:
            raise mmap_error(self.space, v)

    def descr_getitem(self, w_index):
        self.check_valid()

        space = self.space
        start, stop, step, length = space.decode_index4(w_index, self.mmap.size)
        if step == 0:  # index only
            return space.wrap(ord(self.mmap.getitem(start)))
        elif step == 1:
            if stop - start < 0:
                return space.wrapbytes("")
            return space.wrapbytes(self.mmap.getslice(start, length))
        else:
            b = StringBuilder(length)
            for i in range(start, stop, step):
                b.append(self.mmap.getitem(i))
            return space.wrapbytes(b.build())

    def descr_setitem(self, w_index, w_value):
        space = self.space
        self.check_valid()
        self.check_writeable()

        start, stop, step, length = space.decode_index4(w_index, self.mmap.size)
        if step == 0:  # index only
            value = space.int_w(w_value)
            if not 0 <= value < 256:
                raise OperationError(space.w_ValueError, space.wrap(
                        "mmap item value must be in range(0, 256)"))
            self.mmap.setitem(start, chr(value))
        else:
            value = space.bytes_w(w_value)
            if len(value) != length:
                raise OperationError(space.w_ValueError,
                          space.wrap("mmap slice assignment is wrong size"))
            if step == 1:
                self.mmap.setslice(start, value)
            else:
                for i in range(length):
                    self.mmap.setitem(start, value[i])
                    start += step

    def descr_enter(self, space):
        self.check_valid()
        return space.wrap(self)

    def descr_exit(self, space, __args__):
        self.close()


if rmmap._POSIX:

    @unwrap_spec(fileno=int, length=int, flags=int,
                 prot=int, access=int, offset=OFF_T)
    def mmap(space, w_subtype, fileno, length, flags=rmmap.MAP_SHARED,
             prot=rmmap.PROT_WRITE | rmmap.PROT_READ,
             access=rmmap._ACCESS_DEFAULT, offset=0):
        self = space.allocate_instance(W_MMap, w_subtype)
        try:
            W_MMap.__init__(self, space,
                            rmmap.mmap(fileno, length, flags, prot, access,
                                       offset))
        except OSError, e:
            raise mmap_error(space, e)
        except RMMapError, e:
            raise mmap_error(space, e)
        return space.wrap(self)

elif rmmap._MS_WINDOWS:

    @unwrap_spec(fileno=int, length=int, tagname=str,
                 access=int, offset=OFF_T)
    def mmap(space, w_subtype, fileno, length, tagname="",
             access=rmmap._ACCESS_DEFAULT, offset=0):
        self = space.allocate_instance(W_MMap, w_subtype)
        try:
            W_MMap.__init__(self, space,
                            rmmap.mmap(fileno, length, tagname, access,
                                       offset))
        except OSError, e:
            raise mmap_error(space, e)
        except RMMapError, e:
            raise mmap_error(space, e)
        return space.wrap(self)

W_MMap.typedef = TypeDef("mmap.mmap",
    __new__ = interp2app(mmap),
    close = interp2app(W_MMap.close),
    read_byte = interp2app(W_MMap.read_byte),
    readline = interp2app(W_MMap.readline),
    read = interp2app(W_MMap.read),
    find = interp2app(W_MMap.find),
    rfind = interp2app(W_MMap.rfind),
    seek = interp2app(W_MMap.seek),
    tell = interp2app(W_MMap.tell),
    size = interp2app(W_MMap.descr_size),
    write = interp2app(W_MMap.write),
    write_byte = interp2app(W_MMap.write_byte),
    flush = interp2app(W_MMap.flush),
    move = interp2app(W_MMap.move),
    resize = interp2app(W_MMap.resize),

    __len__ = interp2app(W_MMap.__len__),
    __getitem__ = interp2app(W_MMap.descr_getitem),
    __setitem__ = interp2app(W_MMap.descr_setitem),
    __enter__ = interp2app(W_MMap.descr_enter),
    __exit__ = interp2app(W_MMap.descr_exit),

    closed = GetSetProperty(W_MMap.closed_get),
)

constants = rmmap.constants
PAGESIZE = rmmap.PAGESIZE
ALLOCATIONGRANULARITY = rmmap.ALLOCATIONGRANULARITY
ACCESS_READ  = rmmap.ACCESS_READ
ACCESS_WRITE = rmmap.ACCESS_WRITE
ACCESS_COPY  = rmmap.ACCESS_COPY


def mmap_error(space, e):
    if isinstance(e, RValueError):
        return OperationError(space.w_ValueError,
                              space.wrap(e.message))
    elif isinstance(e, RTypeError):
        return OperationError(space.w_TypeError,
                              space.wrap(e.message))
    elif isinstance(e, OSError):
        return wrap_oserror(space, e)
    else:
        # bogus 'e'?
        return OperationError(space.w_SystemError, space.wrap('%s' % e))
mmap_error._dont_inline_ = True


class MMapBuffer(Buffer):
    _immutable_ = True

    def __init__(self, space, mmap, readonly):
        self.space = space
        self.mmap = mmap
        self.readonly = readonly

    def getlength(self):
        return self.mmap.size

    def getitem(self, index):
        self.check_valid()
        return self.mmap.data[index]

    def getslice(self, start, stop, step, size):
        self.check_valid()
        if step == 1:
            return self.mmap.getslice(start, size)
        else:
            return Buffer.getslice(self, start, stop, step, size)

    def setitem(self, index, char):
        self.check_valid_writeable()
        self.mmap.data[index] = char

    def setslice(self, start, string):
        self.check_valid_writeable()
        self.mmap.setslice(start, string)

    def get_raw_address(self):
        self.check_valid()
        return self.mmap.data

    def check_valid(self):
        try:
            self.mmap.check_valid()
        except RValueError, v:
            raise mmap_error(self.space, v)

    def check_valid_writeable(self):
        try:
            self.mmap.check_valid()
            self.mmap.check_writeable()
        except RMMapError, v:
            raise mmap_error(self.space, v)
