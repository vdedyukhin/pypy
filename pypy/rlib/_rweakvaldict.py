from pypy.objspace.flow.model import Constant
from pypy.rpython.lltypesystem import lltype, llmemory, rstr, rclass, rdict
from pypy.rpython.lltypesystem.llmemory import weakref_create, weakref_deref
from pypy.rpython.lltypesystem.lloperation import llop
from pypy.rpython.rclass import getinstancerepr
from pypy.rpython.rint import signed_repr
from pypy.rpython.rmodel import Repr
from pypy.rlib.rweakref import RWeakValueDictionary
from pypy.rlib import jit


class WeakValueDictRepr(Repr):
    def __init__(self, rtyper, r_key):
        self.rtyper = rtyper
        self.r_key = r_key
        self.traits = make_WEAKDICT(r_key)
        self.lowleveltype = lltype.Ptr(self.traits.WEAKDICT)
        self.dict_cache = {}

    def convert_const(self, weakdict):
        if not isinstance(weakdict, RWeakValueDictionary):
            raise TyperError("expected an RWeakValueDictionary: %r" % (
                weakdict,))
        try:
            key = Constant(weakdict)
            return self.dict_cache[key]
        except KeyError:
            self.setup()
            l_dict = self.traits.ll_new_weakdict()
            self.dict_cache[key] = l_dict
            bk = self.rtyper.annotator.bookkeeper
            classdef = bk.getuniqueclassdef(weakdict._valueclass)
            r_value = getinstancerepr(self.rtyper, classdef)
            for dictkey, dictvalue in weakdict._dict.items():
                llkey = self.r_key.convert_const(dictkey)
                llvalue = r_value.convert_const(dictvalue)
                if llvalue:
                    llvalue = lltype.cast_pointer(rclass.OBJECTPTR, llvalue)
                    self.traits.ll_set_nonnull(l_dict, llkey, llvalue)
            return l_dict

    def rtype_method_get(self, hop):
        
        v_d, v_key = hop.inputargs(self, self.r_key)
        hop.exception_cannot_occur()
        v_result = hop.gendirectcall(self.traits.ll_get, v_d, v_key)
        v_result = hop.genop("cast_pointer", [v_result],
                             resulttype=hop.r_result.lowleveltype)
        return v_result

    def rtype_method_set(self, hop):
        r_object = getinstancerepr(self.rtyper, None)
        v_d, v_key, v_value = hop.inputargs(self, self.r_key, r_object)
        hop.exception_cannot_occur()
        if hop.args_s[2].is_constant() and hop.args_s[2].const is None:
            hop.gendirectcall(self.traits.ll_set_null, v_d, v_key)
        else:
            hop.gendirectcall(self.traits.ll_set, v_d, v_key, v_value)


def specialize_make_weakdict(hop, traits):
    hop.exception_cannot_occur()
    v_d = hop.gendirectcall(traits.ll_new_weakdict)
    return v_d

# ____________________________________________________________

def make_WEAKDICT(r_key):
    KEY = r_key.lowleveltype
    ll_keyhash = r_key.get_ll_hash_function()
    if isinstance(KEY, lltype.Ptr):
        zero_key = r_key.convert_const(None)
    else:
        zero_key = r_key.convert_const(0)

    WEAKDICTENTRY = lltype.Struct("weakdictentry",
                                  ("key", KEY),
                                  ("value", llmemory.WeakRefPtr))

    def ll_valid(entries, i):
        value = entries[i].value
        return bool(value) and bool(weakref_deref(rclass.OBJECTPTR, value))

    def ll_everused(entries, i):
        return bool(entries[i].value)

    def ll_hash(entries, i):
        return fasthashfn(entries[i].key)
    fasthashfn = r_key.get_ll_fasthash_function()

    entrymeths = {
        'allocate': lltype.typeMethod(rdict._ll_malloc_entries),
        'delete': rdict._ll_free_entries,
        'valid': ll_valid,
        'everused': ll_everused,
        'hash': ll_hash,
        }
    WEAKDICTENTRYARRAY = lltype.GcArray(WEAKDICTENTRY,
                                        adtmeths=entrymeths,
                                        hints={'weakarray': 'value'})
    # NB. the 'hints' is not used so far ^^^

    class Traits:
        @staticmethod
        @jit.dont_look_inside
        def ll_new_weakdict():
            d = lltype.malloc(Traits.WEAKDICT)
            d.entries = Traits.WEAKDICT.entries.TO.allocate(rdict.DICT_INITSIZE)
            d.num_items = 0
            d.num_pristine_entries = rdict.DICT_INITSIZE
            return d

        @staticmethod
        @jit.dont_look_inside
        def ll_get(d, llkey):
            hash = ll_keyhash(llkey)
            i = rdict.ll_dict_lookup(d, llkey, hash)
            #llop.debug_print(lltype.Void, i, 'get')
            valueref = d.entries[i].value
            if valueref:
                return weakref_deref(rclass.OBJECTPTR, valueref)
            else:
                return lltype.nullptr(rclass.OBJECTPTR.TO)

        @staticmethod
        @jit.dont_look_inside
        def ll_set(d, llkey, llvalue):
            if llvalue:
                Traits.ll_set_nonnull(d, llkey, llvalue)
            else:
                Traits.ll_set_null(d, llkey)

        @staticmethod
        @jit.dont_look_inside
        def ll_set_nonnull(d, llkey, llvalue):
            hash = ll_keyhash(llkey)
            valueref = weakref_create(llvalue)    # GC effects here, before the rest
            i = rdict.ll_dict_lookup(d, llkey, hash)
            everused = d.entries.everused(i)
            d.entries[i].key = llkey
            d.entries[i].value = valueref
            #llop.debug_print(lltype.Void, i, 'stored')
            if not everused:
                d.num_pristine_entries -= 1
                if d.num_pristine_entries * 3 <= len(d.entries):
                    #llop.debug_print(lltype.Void, 'RESIZE')
                    Traits.ll_weakdict_resize(d)

        @staticmethod
        @jit.dont_look_inside
        def ll_set_null(d, llkey):
            hash = ll_keyhash(llkey)
            i = rdict.ll_dict_lookup(d, llkey, hash)
            if d.entries.everused(i):
                # If the entry was ever used, clean up its key and value.
                # We don't store a NULL value, but a dead weakref, because
                # the entry must still be marked as everused().
                d.entries[i].value = llmemory.dead_wref
                d.entries[i].key = zero_key
                #llop.debug_print(lltype.Void, i, 'zero')

        @staticmethod
        def ll_weakdict_resize(d):
            # first set num_items to its correct, up-to-date value
            entries = d.entries
            num_items = 0
            for i in range(len(entries)):
                if entries.valid(i):
                    num_items += 1
            d.num_items = num_items
            rdict.ll_dict_resize(d)

        ll_keyeq = lltype.staticAdtMethod(r_key.get_ll_eq_function())

        dictmeths = {
            'll_get': ll_get,
            'll_set': ll_set,
            'keyeq': ll_keyeq,
            'paranoia': False,
            }

        WEAKDICT = lltype.GcStruct("weakvaldict",
                                   ("num_items", lltype.Signed),
                                   ("num_pristine_entries", lltype.Signed),
                                   ("entries", lltype.Ptr(WEAKDICTENTRYARRAY)),
                                   adtmeths=dictmeths)

    return Traits
