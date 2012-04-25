from pypy.rpython.lltypesystem import lltype, llmemory, llarena, rffi
from pypy.rpython.lltypesystem.lloperation import llop
from pypy.rpython.annlowlevel import cast_instance_to_base_ptr, llhelper
from pypy.rpython.annlowlevel import cast_base_ptr_to_instance, base_ptr_lltype
from pypy.rlib.objectmodel import we_are_translated, free_non_gc_object
from pypy.rlib.objectmodel import specialize
from pypy.rlib.rarithmetic import r_uint
from pypy.rlib.debug import ll_assert, debug_start, debug_stop, fatalerror

from pypy.rpython.memory.gc.stmgc import WORD, NULL
from pypy.rpython.memory.gc.stmgc import always_inline, dont_inline
from pypy.rpython.memory.gc.stmgc import GCFLAG_GLOBAL, GCFLAG_VISITED
from pypy.rpython.memory.gc.stmgc import GCFLAG_WAS_COPIED, GCFLAG_HAS_SHADOW


class StmGCTLS(object):
    """The thread-local structure: we have one instance of these per thread,
    including one for the main thread."""

    _alloc_flavor_ = 'raw'

    nontranslated_dict = {}

    def __init__(self, gc, in_main_thread):
        self.gc = gc
        self.in_main_thread = in_main_thread
        self.stm_operations = self.gc.stm_operations
        self.null_address_dict = self.gc.null_address_dict
        self.AddressStack = self.gc.AddressStack
        self.AddressDict = self.gc.AddressDict
        #
        # --- current position and end of nursery, or NULL when
        #     mallocs are forbidden
        self.nursery_free = NULL
        self.nursery_top  = NULL
        self.nursery_pending_clear = 0
        # --- the start and size of the nursery belonging to this thread.
        #     never changes.
        self.nursery_size  = self.gc.nursery_size
        self.nursery_start = self._alloc_nursery(self.nursery_size)
        #
        # --- a thread-local allocator for the shared area
        from pypy.rpython.memory.gc.stmshared import StmGCThreadLocalAllocator
        self.sharedarea_tls = StmGCThreadLocalAllocator(self.gc.sharedarea)
        # --- the LOCAL objects with GCFLAG_WAS_COPIED
        self.copied_local_objects = self.AddressStack()
        # --- the LOCAL objects which are weakrefs.  They are also listed
        #     in the appropriate place, like sharedarea_tls, if needed.
        self.local_weakrefs = self.AddressStack()
        # --- main thread only: this is the list of GLOBAL objects that
        #     have been turned into LOCAL objects
        if in_main_thread:
            self.mt_global_turned_local = NULL
        #
        self._register_with_C_code()

    def teardown_thread(self):
        self._cleanup_state()
        self._unregister_with_C_code()
        self.local_weakrefs.delete()
        self.copied_local_objects.delete()
        self.sharedarea_tls.delete()
        self._free_nursery(self.nursery_start)
        free_non_gc_object(self)

    def _alloc_nursery(self, nursery_size):
        nursery = llarena.arena_malloc(nursery_size, 1)
        if not nursery:
            raise MemoryError("cannot allocate nursery")
        return nursery

    def _free_nursery(self, nursery):
        llarena.arena_free(nursery)

    def _register_with_C_code(self):
        if we_are_translated():
            tls = cast_instance_to_base_ptr(self)
            tlsaddr = llmemory.cast_ptr_to_adr(tls)
        else:
            n = 10000 + len(StmGCTLS.nontranslated_dict)
            tlsaddr = rffi.cast(llmemory.Address, n)
            StmGCTLS.nontranslated_dict[n] = self
        self.stm_operations.set_tls(tlsaddr, int(self.in_main_thread))

    def _unregister_with_C_code(self):
        ll_assert(self.gc.get_tls() is self,
                  "unregister_with_C_code: wrong thread")
        self.stm_operations.del_tls()

    @staticmethod
    @always_inline
    def cast_address_to_tls_object(tlsaddr):
        if we_are_translated():
            tls = llmemory.cast_adr_to_ptr(tlsaddr, base_ptr_lltype())
            return cast_base_ptr_to_instance(StmGCTLS, tls)
        else:
            n = rffi.cast(lltype.Signed, tlsaddr)
            return StmGCTLS.nontranslated_dict[n]

    def _disable_mallocs(self):
        ll_assert(bool(self.nursery_free), "disable_mallocs: already disabled")
        self.nursery_pending_clear = self.nursery_free - self.nursery_start
        self.nursery_free = NULL
        self.nursery_top  = NULL

    # ------------------------------------------------------------

    def enter_transactional_mode(self):
        """Called on the main thread, just before spawning the other
        threads."""
        self.stop_transaction()
        #
        # We must also mark the following objects as GLOBAL again
        obj = self.mt_global_turned_local
        self.mt_global_turned_local = NULL
        while obj:
            hdr = self.gc.header(obj)
            obj = hdr.version
            ll_assert(hdr.tid & GCFLAG_GLOBAL == 0, "already GLOBAL [2]")
            ll_assert(hdr.tid & GCFLAG_VISITED != 0, "missing VISITED [2]")
            hdr.tid += GCFLAG_GLOBAL - GCFLAG_VISITED
            self._clear_version_for_global_object(hdr)
        if not we_are_translated():
            del self.mt_global_turned_local   # don't use any more

    def leave_transactional_mode(self):
        """Restart using the main thread for mallocs."""
        if not we_are_translated():
            for key, value in StmGCTLS.nontranslated_dict.items():
                if value is not self:
                    del StmGCTLS.nontranslated_dict[key]
        self.start_transaction()
        #
        # Do something special here after we restarted the "transaction"
        # in the main thread.  At this point, *all* objects are GLOBAL.
        # The write_barrier will ensure that any write makes the written-to
        # objects LOCAL again.  However, it is possible that the write
        # barrier was called before the enter/leave_transactional_mode()
        # and will not be called again before writing.  But such objects
        # are right now directly in the stack.  So to fix this issue, we
        # conservatively mark as local all objects directly from the stack.
        # XXX TODO: do the same thing after each local_collection() by
        # the main thread.
        self.mt_global_turned_local = NULL
        self.gc.root_walker.walk_current_stack_roots(
            StmGCTLS._remark_object_as_local, self)

    def start_transaction(self):
        """Start a transaction: performs any pending cleanups, and set
        up a fresh state for allocating.  Called at the start of
        each transaction, and at the start of the main thread."""
        # Note that the calls to enter() and
        # end_of_transaction_collection() are not balanced: if a
        # transaction is aborted, the latter might never be called.
        # Be ready here to clean up any state.
        self._cleanup_state()
        if self.nursery_free:
            clear_size = self.nursery_free - self.nursery_start
        else:
            clear_size = self.nursery_pending_clear
        self.nursery_pending_clear = 0
        if clear_size > 0:
            llarena.arena_reset(self.nursery_start, clear_size, 2)
        self.nursery_free = self.nursery_start
        self.nursery_top  = self.nursery_start + self.nursery_size

    def stop_transaction(self):
        """Stop a transaction: do a local collection to empty the
        nursery and track which objects are still alive now, and
        then mark all these objects as global."""
        self.local_collection(end_of_transaction=1)
        if not self.local_nursery_is_empty():
            self.local_collection(end_of_transaction=2)
        self._promote_locals_to_globals()
        self._disable_mallocs()

    def local_nursery_is_empty(self):
        ll_assert(bool(self.nursery_free),
                  "local_nursery_is_empty: gc not running")
        return self.nursery_free == self.nursery_start

    # ------------------------------------------------------------

    def local_collection(self, end_of_transaction=0):
        """Do a local collection.  This should be equivalent to a minor
        collection only, but the GC is not generational so far, so it is
        for now the same as a full collection --- but only on LOCAL
        objects, not touching the GLOBAL objects.  More precisely, this
        finds all YOUNG LOCAL objects, move them out of the nursery if
        necessary, and make them OLD LOCAL objects.  This starts from
        the roots from the stack.  The flag GCFLAG_WAS_COPIED is kept
        and the C tree is updated if the local young objects move.
        """
        #
        debug_start("gc-local")
        #
        if end_of_transaction:
            self.detect_flag_combination = GCFLAG_WAS_COPIED | GCFLAG_VISITED
        else:
            self.detect_flag_combination = -1
        #
        # Move away the previous sharedarea_tls and start a new one.
        from pypy.rpython.memory.gc.stmshared import StmGCThreadLocalAllocator
        previous_sharedarea_tls = self.sharedarea_tls
        self.sharedarea_tls = StmGCThreadLocalAllocator(self.gc.sharedarea)
        #
        # List of LOCAL objects pending a visit.  Note that no GLOBAL
        # object can at any point contain a reference to a LOCAL object.
        self.pending = self.AddressStack()
        #
        # First, find the roots that point to LOCAL objects.  All YOUNG
        # (i.e. nursery) objects found are copied out of the nursery.
        # All OLD objects found are flagged with GCFLAG_VISITED.
        # At this point, the content of the objects is not modified;
        # they are simply added to 'pending'.
        self.collect_roots_from_stack()
        #
        # Also find the roots that are the local copy of GCFLAG_WAS_COPIED
        # objects.
        if not self.in_main_thread:
            self.collect_roots_from_tldict()
        else:
            self.collect_from_mt_global_turned_local()
        #
        # Now repeatedly follow objects until 'pending' is empty.
        self.collect_flush_pending()
        #
        # Walk the list of LOCAL weakrefs, and update it if necessary.
        if self.local_weakrefs.non_empty():
            self.update_local_weakrefs()
        #
        # Visit all previous OLD objects.  Free the ones that have not been
        # visited above, and reset GCFLAG_VISITED on the others.
        self.mass_free_old_local(previous_sharedarea_tls)
        #
        # Note that the last step guarantees the invariant that between
        # collections, all the objects linked within 'self.sharedarea_tls'
        # don't have GCFLAG_VISITED.  As the newly allocated nursery
        # objects don't have it either, at the start of the next
        # collection, the only LOCAL objects that have it are the ones
        # in 'mt_global_turned_local' or the C tldict with GCFLAG_WAS_COPIED.
        #
        # All live nursery objects are out, and the rest dies.  Fill
        # the whole nursery with zero and reset the current nursery pointer.
        ll_assert(bool(self.nursery_free), "nursery_free is NULL")
        size_used = self.nursery_free - self.nursery_start
        llarena.arena_reset(self.nursery_start, size_used, 2)
        self.nursery_free = self.nursery_start
        #
        debug_stop("gc-local")

    # ------------------------------------------------------------

    @always_inline
    def allocate_bump_pointer(self, size):
        free = self.nursery_free
        top  = self.nursery_top
        if (top - free) < llmemory.raw_malloc_usage(size):
            free = self.allocate_object_of_size(size)
        self.nursery_free = free + size
        return free

    @dont_inline
    def allocate_object_of_size(self, size):
        if not self.nursery_free:
            fatalerror("malloc in a non-main thread but outside a transaction")
        if llmemory.raw_malloc_usage(size) > self.nursery_size // 8 * 7:
            fatalerror("object too large to ever fit in the nursery")
        while True:
            self.local_collection()
            free = self.nursery_free
            top  = self.nursery_top
            if (top - free) < llmemory.raw_malloc_usage(size):
                continue         # try again
            return free

    def is_in_nursery(self, addr):
        ll_assert(llmemory.cast_adr_to_int(addr) & 1 == 0,
                  "odd-valued (i.e. tagged) pointer unexpected here")
        return self.nursery_start <= addr < self.nursery_top

    def malloc_local_copy(self, totalsize):
        """Allocate an object that will be used as a LOCAL copy of
        some GLOBAL object."""
        localobj = self.sharedarea_tls.malloc_object(totalsize)
        self.copied_local_objects.append(localobj)
        return localobj

    def fresh_new_weakref(self, obj):
        self.local_weakrefs.append(obj)

    def _remark_object_as_local(self, root):
        obj = root.address[0]
        hdr = self.gc.header(obj)
        if hdr.tid & GCFLAG_GLOBAL:
            self.main_thread_writes_to_global_obj(obj)

    def main_thread_writes_to_global_obj(self, obj):
        hdr = self.gc.header(obj)
        # XXX should we also remove GCFLAG_WAS_COPIED here if it is set?
        ll_assert(hdr.tid & GCFLAG_VISITED == 0,
                  "write in main thread: unexpected GCFLAG_VISITED")
        # remove GCFLAG_GLOBAL, and add GCFLAG_VISITED
        hdr.tid += (GCFLAG_VISITED - GCFLAG_GLOBAL)
        # add the object into this linked list
        hdr.version = self.mt_global_turned_local
        self.mt_global_turned_local = obj

    # ------------------------------------------------------------

    def _promote_locals_to_globals(self):
        ll_assert(self.local_nursery_is_empty(), "nursery must be empty [1]")
        #
        # Promote all objects in sharedarea_tls to global
        obj = self.sharedarea_tls.chained_list
        self.sharedarea_tls.chained_list = NULL
        #
        while obj:
            hdr = self.gc.header(obj)
            obj = hdr.version
            ll_assert(hdr.tid & GCFLAG_GLOBAL == 0, "already GLOBAL [1]")
            ll_assert(hdr.tid & GCFLAG_VISITED == 0, "unexpected VISITED [1]")
            hdr.tid |= GCFLAG_GLOBAL
            self._clear_version_for_global_object(hdr)

    def _clear_version_for_global_object(self, hdr):
        # Reset the 'version' to initialize a newly global object.
        # When translated with C code, we set it to NULL (version 0).
        # When non-translated, we reset it instead to '_uninitialized'
        # to simulate the fact that the C code might change it.
        if we_are_translated():
            hdr.version = NULL
        else:
            del hdr.version

    def _cleanup_state(self):
        #if self.rawmalloced_objects:
        #    xxx     # free the rawmalloced_objects still around

        # free the old unused local objects still allocated in the
        # StmGCThreadLocalAllocator
        self.sharedarea_tls.free_and_clear()
        # free the local copies.  Note that commonly, they are leftovers
        # from the previous transaction running in this thread.  The C code
        # has just copied them over the corresponding GLOBAL objects at the
        # very end of that transaction.
        self._free_and_clear_list(self.copied_local_objects)
        # forget the local weakrefs.
        self.local_weakrefs.clear()

    def _free_and_clear_list(self, lst):
        while lst.non_empty():
            self.sharedarea_tls.free_object(lst.pop())


    def collect_roots_from_stack(self):
        self.gc.root_walker.walk_current_stack_roots(
            StmGCTLS._trace_drag_out1, self)

    def trace_and_drag_out_of_nursery(self, obj):
        # This is called to fix the references inside 'obj', to ensure that
        # they are global.  If necessary, the referenced objects are copied
        # into the global area first.  This is called on the LOCAL copy of
        # the roots, and on the freshly OLD copy of all other reached LOCAL
        # objects.
        self.gc.trace(obj, self._trace_drag_out, None)

    def _trace_drag_out1(self, root):
        self._trace_drag_out(root, None)

    @always_inline
    def categorize_object(self, obj, can_be_in_nursery):
        """Return the current surviving state of the object:
            0: not marked as surviving, so far
            1: survives and does not move
            2: survives, but moves to 'hdr.version'
        """
        hdr = self.gc.header(obj)
        flag_combination = hdr.tid & (GCFLAG_GLOBAL |
                                      GCFLAG_WAS_COPIED |
                                      GCFLAG_VISITED)
        if flag_combination == 0:
            return 0    # not marked as surviving, so far

        if flag_combination == self.detect_flag_combination:
            # At a normal time, self.detect_flag_combination is -1
            # and this case is never seen.  At end of transactions,
            # detect_flag_combination is GCFLAG_WAS_COPIED|GCFLAG_VISITED.
            # This case is to force pointers to the LOCAL copy to be
            # replaced with pointers to the GLOBAL copy.
            return 2

        if can_be_in_nursery and self.is_in_nursery(obj):
            return 2
        else:
            return 1

    def _trace_drag_out(self, root, ignored):
        """Trace callback: 'root' is the address of some pointer.  If that
        pointer points to a YOUNG object, allocate an OLD copy of it and
        fix the pointer.  Also, add the object to the 'pending' stack, if
        it was not done so far.
        """
        obj = root.address[0]
        size = self.gc.get_size(obj)
        # ^^^ moved here in order to crash early if 'obj' is invalid
        hdr = self.gc.header(obj)
        #
        # If 'obj' is not in the nursery, we set GCFLAG_VISITED
        if not self.is_in_nursery(obj):
            # we ignore both GLOBAL objects and objects which have already
            # been VISITED
            cat = self.categorize_object(obj, can_be_in_nursery=False)
            if cat == 0:
                hdr.tid |= GCFLAG_VISITED
                self.pending.append(obj)
            elif cat == 2:
                root.address[0] = hdr.version
            return
        #
        # If 'obj' was already forwarded, change it to its forwarding address.
        # If 'obj' has already a shadow but isn't forwarded so far, use it.
        if hdr.tid & (GCFLAG_VISITED | GCFLAG_HAS_SHADOW):
            #
            if hdr.tid & GCFLAG_VISITED:
                root.address[0] = hdr.version
                return
            #
            # Case of GCFLAG_HAS_SHADOW.  See comments below.
            size_gc_header = self.gc.gcheaderbuilder.size_gc_header
            totalsize = size_gc_header + size
            hdr.tid &= ~GCFLAG_HAS_SHADOW
            newobj = hdr.version
            newhdr = self.gc.header(newobj)
            #
            saved_version = newhdr.version
            llmemory.raw_memcopy(obj - size_gc_header,
                                 newobj - size_gc_header,
                                 totalsize)
            newhdr.version = saved_version
            newhdr.tid = hdr.tid | GCFLAG_VISITED
            #
        else:
            #
            # First visit to 'obj': we must move this YOUNG obj out of the
            # nursery.
            size_gc_header = self.gc.gcheaderbuilder.size_gc_header
            totalsize = size_gc_header + size
            #
            # Common case: allocate a new nonmovable location for it.
            newobj = self._malloc_out_of_nursery(totalsize)
            #
            # Copy it.  Note that references to other objects in the
            # nursery are kept unchanged in this step.
            llmemory.raw_memcopy(obj - size_gc_header,
                                 newobj - size_gc_header,
                                 totalsize)
            #
            # Register the object here, not before the memcopy() that would
            # overwrite its 'version' field
            self._register_newly_malloced_obj(newobj)
        #
        # Set the YOUNG copy's GCFLAG_VISITED and set its version to
        # point to the OLD copy.
        hdr.tid |= GCFLAG_VISITED
        hdr.version = newobj
        #
        # Change the original pointer to this object.
        root.address[0] = newobj
        #
        # Add the newobj to the list 'pending', because it can contain
        # further pointers to other young objects.  We will fix such
        # references to point to the copy of the young objects when we
        # walk 'pending_list'.
        self.pending.append(newobj)

    def _malloc_out_of_nursery(self, totalsize):
        return self.sharedarea_tls.malloc_object(totalsize)

    def _register_newly_malloced_obj(self, obj):
        self.sharedarea_tls.add_regular(obj)

    def collect_roots_from_tldict(self):
        if not we_are_translated():
            if not hasattr(self.stm_operations, 'tldict_enum'):
                return
        CALLBACK = self.stm_operations.CALLBACK_ENUM
        llop.nop(lltype.Void, llhelper(CALLBACK, StmGCTLS._stm_enum_callback))
        # The previous line causes the _stm_enum_callback() function to be
        # generated in the C source with a specific signature, where it
        # can be called by the C code.
        self.stm_operations.tldict_enum()

    @staticmethod
    def _stm_enum_callback(tlsaddr, globalobj, localobj):
        self = StmGCTLS.cast_address_to_tls_object(tlsaddr)
        localhdr = self.gc.header(localobj)
        ll_assert(localhdr.version == globalobj,
                  "in a root: localobj.version != globalobj")
        ll_assert(localhdr.tid & GCFLAG_GLOBAL == 0,
                  "in a root: unexpected GCFLAG_GLOBAL")
        ll_assert(localhdr.tid & GCFLAG_WAS_COPIED != 0,
                  "in a root: missing GCFLAG_WAS_COPIED")
        ll_assert(localhdr.tid & GCFLAG_VISITED != 0,
                  "in a root: missing GCFLAG_VISITED")
        globalhdr = self.gc.header(globalobj)
        ll_assert(globalhdr.tid & GCFLAG_GLOBAL != 0,
                  "in a root: GLOBAL: missing GCFLAG_GLOBAL")
        ll_assert(globalhdr.tid & GCFLAG_WAS_COPIED != 0,
                  "in a root: GLOBAL: missing GCFLAG_WAS_COPIED")
        ll_assert(globalhdr.tid & GCFLAG_VISITED == 0,
                  "in a root: GLOBAL: unexpected GCFLAG_VISITED")
        TL = lltype.cast_primitive(lltype.Signed,
                                   self.gc.get_type_id(localobj))
        TG = lltype.cast_primitive(lltype.Signed,
                                   self.gc.get_type_id(globalobj))
        ll_assert(TL == TG, "in a root: type(LOCAL) != type(GLOBAL)")
        #
        self.trace_and_drag_out_of_nursery(localobj)

    def collect_from_mt_global_turned_local(self):
        # NB. all objects in the 'mt_global_turned_local' list are
        # currently immortal (because they were once GLOBAL)
        obj = self.mt_global_turned_local
        while obj:
            hdr = self.gc.header(obj)
            ll_assert(hdr.tid & GCFLAG_GLOBAL == 0,
                      "unexpected GCFLAG_GLOBAL in mt_global_turned_local")
            ll_assert(hdr.tid & GCFLAG_VISITED != 0,
                      "missing GCFLAG_VISIBLE in mt_global_turned_local")
            self.trace_and_drag_out_of_nursery(obj)
            obj = hdr.version

    def collect_flush_pending(self):
        # Follow the objects in the 'pending' stack and move the
        # young objects they point to out of the nursery.
        while self.pending.non_empty():
            obj = self.pending.pop()
            self.trace_and_drag_out_of_nursery(obj)
        self.pending.delete()

    def update_local_weakrefs(self):
        old = self.local_weakrefs
        new = self.AddressStack()
        while old.non_empty():
            obj = old.pop()
            hdr = self.gc.header(obj)
            ll_assert(hdr.tid & (GCFLAG_GLOBAL|GCFLAG_WAS_COPIED) == 0,
                      "local weakref: bad flags")
            if hdr.tid & GCFLAG_VISITED == 0:
                continue # weakref itself dies
            #
            if self.is_in_nursery(obj):
                obj = hdr.version
                #hdr = self.gc.header(obj) --- not needed any more
            offset = self.gc.weakpointer_offset(self.gc.get_type_id(obj))
            pointing_to = (obj + offset).address[0]
            ll_assert(bool(pointing_to), "weakref to NULL in local_weakrefs")
            cat = self.categorize_object(pointing_to, can_be_in_nursery=True)
            if cat == 0:
                # the weakref points to a dying object; no need to rememeber it
                (obj + offset).address[0] = llmemory.NULL
            else:
                # the weakref points to an object that stays alive
                if cat == 2:      # update the pointer if needed
                    pointing_hdr = self.gc.header(pointing_to)
                    (obj + offset).address[0] = pointing_hdr.version
                new.append(obj)   # re-register in the new local_weakrefs list
        #
        self.local_weakrefs = new
        old.delete()

    def mass_free_old_local(self, previous_sharedarea_tls):
        obj = previous_sharedarea_tls.chained_list
        previous_sharedarea_tls.delete()
        while obj != NULL:
            hdr = self.gc.header(obj)
            next = hdr.version
            if hdr.tid & GCFLAG_VISITED:
                # survives: relink in the new sharedarea_tls
                hdr.tid -= GCFLAG_VISITED
                self.sharedarea_tls.add_regular(obj)
            else:
                # dies
                self.sharedarea_tls.free_object(obj)
            #
            obj = next