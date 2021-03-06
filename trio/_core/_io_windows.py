import itertools
from contextlib import contextmanager
import enum
import socket

import attr

from .. import _core
from ._run import _public
from ._io_common import wake_all

from ._windows_cffi import (
    ffi,
    kernel32,
    ntdll,
    ws2_32,
    INVALID_HANDLE_VALUE,
    raise_winerror,
    _handle,
    ErrorCodes,
    FileFlags,
    AFDPollFlags,
    WSAIoctls,
    CompletionModes,
    IoControlCodes,
)

# There's a lot to be said about the overall design of a Windows event
# loop. See
#
#    https://github.com/python-trio/trio/issues/52
#
# for discussion. This now just has some lower-level notes:
#
# How IOCP fits together:
#
# The general model is that you call some function like ReadFile or WriteFile
# to tell the kernel that you want it to perform some operation, and the
# kernel goes off and does that in the background, then at some point later it
# sends you a notification that the operation is complete. There are some more
# exotic APIs that don't quite fit this pattern, but most APIs do.
#
# Each background operation is tracked using an OVERLAPPED struct, that
# uniquely identifies that particular operation.
#
# An "IOCP" (or "I/O completion port") is an object that lets the kernel send
# us these notifications -- basically it's just a kernel->userspace queue.
#
# Each IOCP notification is represented by an OVERLAPPED_ENTRY struct, which
# contains 3 fields:
# - The "completion key". This is an opaque integer that we pick, and use
#   however is convenient.
# - pointer to the OVERLAPPED struct for the completed operation.
# - dwNumberOfBytesTransferred (an integer).
#
# And in addition, for regular I/O, the OVERLAPPED structure gets filled in
# with:
# - result code (named "Internal")
# - number of bytes transferred (named "InternalHigh"); usually redundant
#   with dwNumberOfBytesTransferred.
#
# There are also some other entries in OVERLAPPED which only matter on input:
# - Offset and OffsetHigh which are inputs to {Read,Write}File and
#   otherwise always zero
# - hEvent which is for if you aren't using IOCP; we always set it to zero.
#
# That describes the usual pattern for operations and the usual meaning of
# these struct fields, but really these are just some arbitrary chunks of
# bytes that get passed back and forth, so some operations like to overload
# them to mean something else.
#
# You can also directly queue an OVERLAPPED_ENTRY object to an IOCP by calling
# PostQueuedCompletionStatus. When you use this you get to set all the
# OVERLAPPED_ENTRY fields to arbitrary values.
#
# You can request to cancel any operation if you know which handle it was
# issued on + the OVERLAPPED struct that identifies it (via CancelIoEx). This
# request might fail because the operation has already completed, or it might
# be queued to happen in the background, so you only find out whether it
# succeeded or failed later, when we get back the notification for the
# operation being complete.
#
# There are three types of operations that we support:
#
# == Regular I/O operations on handles (e.g. files or named pipes) ==
#
# Implemented by: register_with_iocp, wait_overlapped
#
# To use these, you have to register the handle with your IOCP first. Once
# it's registered, any operations on that handle will automatically send
# completion events to that IOCP, with a completion key that you specify *when
# the handle is registered* (so you can't use different completion keys for
# different operations).
#
# We give these two dedicated completion keys: CKeys.WAIT_OVERLAPPED for
# regular operations, and CKeys.LATE_CANCEL that's used to make
# wait_overlapped cancellable even if the user forgot to call
# register_with_iocp. The problem here is that after we request the cancel,
# wait_overlapped keeps blocking until it sees the completion notification...
# but if the user forgot to register_with_iocp, then the completion will never
# come, so the cancellation will never resolve. To avoid this, whenever we try
# to cancel an I/O operation and the cancellation fails, we use
# PostQueuedCompletionStatus to send a CKeys.LATE_CANCEL notification. If this
# arrives before the real completion, we assume the user forgot to call
# register_with_iocp on their handle, and raise an error accordingly.
#
# == Socket state notifications ==
#
# Implemented by: wait_readable, wait_writable
#
# The public APIs that windows provides for this are all really awkward and
# don't integrate with IOCP. So we drop down to a lower level, and talk
# directly to the socket device driver in the kernel, which is called "AFD".
# Unfortunately, this is a totally undocumented internal API. Fortunately
# libuv also does this, so we can be pretty confident that MS won't break it
# on us, and there is a *little* bit of information out there if you go
# digging.
#
# Basically: we open a magic file that refers to the AFD driver, register the
# magic file with our IOCP, and then we can issue regular overlapped I/O
# operations on that handle. Specifically, the operation we use is called
# IOCTL_AFD_POLL, which lets us pass in a buffer describing which events we're
# interested in on a given socket (readable, writable, etc.). Later, when the
# operation completes, the kernel rewrites the buffer we passed in to record
# which events happened, and uses IOCP as normal to notify us that this
# operation has completed.
#
# Unfortunately, the Windows kernel seems to have bugs if you try to issue
# multiple simultaneous IOCTL_AFD_POLL operations on the same socket (see
# notes-to-self/afd-lab.py). So if a user calls wait_readable and
# wait_writable at the same time, we have to combine those into a single
# IOCTL_AFD_POLL. This means we can't just use the wait_overlapped machinery.
# Instead we have some dedicated code to handle these operations, and a
# dedicated completion key CKeys.AFD_POLL.
#
# Sources of information:
# - https://github.com/python-trio/trio/issues/52
# - Wepoll: https://github.com/piscisaureus/wepoll/
# - libuv: https://github.com/libuv/libuv/
# - ReactOS: https://github.com/reactos/reactos/
# - Ancient leaked copies of the Windows NT and Winsock source code:
#   https://github.com/pustladi/Windows-2000/blob/661d000d50637ed6fab2329d30e31775046588a9/private/net/sockets/winsock2/wsp/msafd/select.c#L59-L655
#   https://github.com/metoo10987/WinNT4/blob/f5c14e6b42c8f45c20fe88d14c61f9d6e0386b8e/private/ntos/afd/poll.c#L68-L707
# - The WSAEventSelect docs (this exposes a finer-grained set of events than
#   select(), so if you squint you can treat it as a source of information on
#   the fine-grained AFD poll types)
#
#
# == Everything else ==
#
# There are also some weirder APIs for interacting with IOCP. For example, the
# "Job" API lets you specify an IOCP handle and "completion key", and then in
# the future whenever certain events happen it sends uses IOCP to send a
# notification. These notifications don't correspond to any particular
# operation; they're just spontaneous messages you get. The
# "dwNumberOfBytesTransferred" field gets repurposed to carry an identifier
# for the message type (e.g. JOB_OBJECT_MSG_EXIT_PROCESS), and the
# "lpOverlapped" field gets repurposed to carry some arbitrary data that
# depends on the message type (e.g. the pid of the process that exited).
#
# To handle these, we have monitor_completion_key, where we hand out an
# unassigned completion key, let users set it up however they want, and then
# get any events that arrive on that key.
#
# (Note: monitor_completion_key is not documented or fully baked; expect it to
# change in the future.)


# Our completion keys
class CKeys(enum.IntEnum):
    AFD_POLL = 0
    WAIT_OVERLAPPED = 1
    LATE_CANCEL = 2
    USER_DEFINED = 3  # and above


def _check(success):
    if not success:
        raise_winerror()
    return success


def _get_base_socket(sock, *, which=WSAIoctls.SIO_BASE_HANDLE):
    if hasattr(sock, "fileno"):
        sock = sock.fileno()
    base_ptr = ffi.new("HANDLE *")
    out_size = ffi.new("DWORD *")
    failed = ws2_32.WSAIoctl(
        ffi.cast("SOCKET", sock),
        which,
        ffi.NULL,
        0,
        base_ptr,
        ffi.sizeof("HANDLE"),
        out_size,
        ffi.NULL,
        ffi.NULL,
    )
    if failed:
        code = ws2_32.WSAGetLastError()
        raise_winerror(code)
    return base_ptr[0]


def _afd_helper_handle():
    # The "AFD" driver is exposed at the NT path "\Device\Afd". We're using
    # the Win32 CreateFile, though, so we have to pass a Win32 path. \\.\ is
    # how Win32 refers to the NT \GLOBAL??\ directory, and GLOBALROOT is a
    # symlink inside that directory that points to the root of the NT path
    # system. So by sticking that in front of the NT path, we get a Win32
    # path. Alternatively, we could use NtCreateFile directly, since it takes
    # an NT path. But we already wrap CreateFileW so this was easier.
    # References:
    #   https://blogs.msdn.microsoft.com/jeremykuhne/2016/05/02/dos-to-nt-a-paths-journey/
    #   https://stackoverflow.com/a/21704022
    #
    # I'm actually not sure what the \Trio part at the end of the path does.
    # Wepoll uses \Device\Afd\Wepoll, so I just copied them. (I'm guessing it
    # might be visible in some debug tools, and is otherwise arbitrary?)
    rawname = r"\\.\GLOBALROOT\Device\Afd\Trio".encode("utf-16le") + b"\0\0"
    rawname_buf = ffi.from_buffer(rawname)

    handle = kernel32.CreateFileW(
        ffi.cast("LPCWSTR", rawname_buf),
        FileFlags.SYNCHRONIZE,
        FileFlags.FILE_SHARE_READ | FileFlags.FILE_SHARE_WRITE,
        ffi.NULL,  # no security attributes
        FileFlags.OPEN_EXISTING,
        FileFlags.FILE_FLAG_OVERLAPPED,
        ffi.NULL,  # no template file
    )
    if handle == INVALID_HANDLE_VALUE:  # pragma: no cover
        raise_winerror()
    return handle


# AFD_POLL has a finer-grained set of events than other APIs. We collapse them
# down into Unix-style "readable" and "writable".
#
# Note: AFD_POLL_LOCAL_CLOSE isn't a reliable substitute for notify_closing(),
# because even if the user closes the socket *handle*, the socket *object*
# could still remain open, e.g. if the socket was dup'ed (possibly into
# another process). Explicitly calling notify_closing() guarantees that
# everyone waiting on the *handle* wakes up, which is what you'd expect.
#
# However, we can't avoid getting LOCAL_CLOSE notifications -- the kernel
# delivers them whether we ask for them or not -- so better to include them
# here for documentation, and so that when we check (delivered & requested) we
# get a match.

READABLE_FLAGS = (
    AFDPollFlags.AFD_POLL_RECEIVE
    | AFDPollFlags.AFD_POLL_ACCEPT
    | AFDPollFlags.AFD_POLL_DISCONNECT  # other side sent an EOF
    | AFDPollFlags.AFD_POLL_ABORT
    | AFDPollFlags.AFD_POLL_LOCAL_CLOSE
)

WRITABLE_FLAGS = (
    AFDPollFlags.AFD_POLL_SEND
    | AFDPollFlags.AFD_POLL_CONNECT_FAIL
    | AFDPollFlags.AFD_POLL_ABORT
    | AFDPollFlags.AFD_POLL_LOCAL_CLOSE
)


# Annoyingly, while the API makes it *seem* like you can happily issue as many
# independent AFD_POLL operations as you want without them interfering with
# each other, in fact if you issue two AFD_POLL operations for the same socket
# at the same time with notification going to the same IOCP port, then Windows
# gets super confused. For example, if we issue one operation from
# wait_readable, and another independent operation from wait_writable, then
# Windows may complete the wait_writable operation when the socket becomes
# readable.
#
# To avoid this, we have to coalesce all the operations on a single socket
# into one, and when the set of waiters changes we have to throw away the old
# operation and start a new one.
@attr.s(slots=True, eq=False)
class AFDWaiters:
    read_task = attr.ib(default=None)
    write_task = attr.ib(default=None)
    current_op = attr.ib(default=None)


# We also need to bundle up all the info for a single op into a standalone
# object, because we need to keep all these objects alive until the operation
# finishes, even if we're throwing it away.
@attr.s(slots=True, eq=False, frozen=True)
class AFDPollOp:
    lpOverlapped = attr.ib()
    poll_info = attr.ib()
    waiters = attr.ib()


@attr.s(slots=True, eq=False, frozen=True)
class _WindowsStatistics:
    tasks_waiting_read = attr.ib()
    tasks_waiting_write = attr.ib()
    tasks_waiting_overlapped = attr.ib()
    completion_key_monitors = attr.ib()
    backend = attr.ib(default="windows")


# Maximum number of events to dequeue from the completion port on each pass
# through the run loop. Somewhat arbitrary. Should be large enough to collect
# a good set of tasks on each loop, but not so large to waste tons of memory.
# (Each WindowsIOManager holds a buffer whose size is ~32x this number.)
MAX_EVENTS = 1000


@attr.s(frozen=True)
class CompletionKeyEventInfo:
    lpOverlapped = attr.ib()
    dwNumberOfBytesTransferred = attr.ib()


class WindowsIOManager:
    def __init__(self):
        # If this method raises an exception, then __del__ could run on a
        # half-initialized object. So we initialize everything that __del__
        # touches to safe values up front, before we do anything that can
        # fail.
        self._iocp = None
        self._afd = None

        self._iocp = _check(
            kernel32.CreateIoCompletionPort(
                INVALID_HANDLE_VALUE, ffi.NULL, 0, 0
            )
        )
        self._events = ffi.new("OVERLAPPED_ENTRY[]", MAX_EVENTS)

        self._afd = _afd_helper_handle()
        self._register_with_iocp(self._afd, CKeys.AFD_POLL)
        # {lpOverlapped: AFDPollOp}
        self._afd_ops = {}
        # {socket handle: AFDWaiters}
        self._afd_waiters = {}

        # {lpOverlapped: task}
        self._overlapped_waiters = {}
        self._posted_too_late_to_cancel = set()

        self._completion_key_queues = {}
        self._completion_key_counter = itertools.count(CKeys.USER_DEFINED)

        with socket.socket() as s:
            # LSPs can't override this.
            base_handle = _get_base_socket(s, which=WSAIoctls.SIO_BASE_HANDLE)
            # LSPs can in theory override this, but we believe that it never
            # actually happens in the wild.
            select_handle = _get_base_socket(
                s, which=WSAIoctls.SIO_BSP_HANDLE_SELECT
            )
            if base_handle != select_handle:  # pragma: no cover
                raise RuntimeError(
                    "Unexpected network configuration detected. "
                    "Please file a bug at "
                    "https://github.com/python-trio/trio/issues/new, "
                    "and include the output of running: "
                    "netsh winsock show catalog"
                )

    def close(self):
        try:
            if self._iocp is not None:
                iocp = self._iocp
                self._iocp = None
                _check(kernel32.CloseHandle(iocp))
        finally:
            if self._afd is not None:
                afd = self._afd
                self._afd = None
                _check(kernel32.CloseHandle(afd))

    def __del__(self):
        self.close()

    def statistics(self):
        tasks_waiting_read = 0
        tasks_waiting_write = 0
        for waiter in self._afd_waiters.values():
            if waiter.read_task is not None:
                tasks_waiting_read += 1
            if waiter.write_task is not None:
                tasks_waiting_write += 1
        return _WindowsStatistics(
            tasks_waiting_read=tasks_waiting_read,
            tasks_waiting_write=tasks_waiting_write,
            tasks_waiting_overlapped=len(self._overlapped_waiters),
            completion_key_monitors=len(self._completion_key_queues),
        )

    def handle_io(self, timeout):
        received = ffi.new("PULONG")
        milliseconds = round(1000 * timeout)
        if timeout > 0 and milliseconds == 0:
            milliseconds = 1
        try:
            _check(
                kernel32.GetQueuedCompletionStatusEx(
                    self._iocp, self._events, MAX_EVENTS, received,
                    milliseconds, 0
                )
            )
        except OSError as exc:
            if exc.winerror != ErrorCodes.WAIT_TIMEOUT:  # pragma: no cover
                raise
            return
        for i in range(received[0]):
            entry = self._events[i]
            if entry.lpCompletionKey == CKeys.AFD_POLL:
                lpo = entry.lpOverlapped
                op = self._afd_ops.pop(lpo)
                waiters = op.waiters
                if waiters.current_op is not op:
                    # Stale op, nothing to do
                    pass
                else:
                    waiters.current_op = None
                    # I don't think this can happen, so if it does let's crash
                    # and get a debug trace.
                    if lpo.Internal != 0:  # pragma: no cover
                        code = ntdll.RtlNtStatusToDosError(lpo.Internal)
                        raise_winerror(code)
                    flags = op.poll_info.Handles[0].Events
                    if waiters.read_task and flags & READABLE_FLAGS:
                        _core.reschedule(waiters.read_task)
                        waiters.read_task = None
                    if waiters.write_task and flags & WRITABLE_FLAGS:
                        _core.reschedule(waiters.write_task)
                        waiters.write_task = None
                    self._refresh_afd(op.poll_info.Handles[0].Handle)
            elif entry.lpCompletionKey == CKeys.WAIT_OVERLAPPED:
                # Regular I/O event, dispatch on lpOverlapped
                waiter = self._overlapped_waiters.pop(entry.lpOverlapped)
                _core.reschedule(waiter)
            elif entry.lpCompletionKey == CKeys.LATE_CANCEL:
                # Post made by a regular I/O event's abort_fn
                # after it failed to cancel the I/O. If we still
                # have a waiter with this lpOverlapped, we didn't
                # get the regular I/O completion and almost
                # certainly the user forgot to call
                # register_with_iocp.
                self._posted_too_late_to_cancel.remove(entry.lpOverlapped)
                try:
                    waiter = self._overlapped_waiters.pop(entry.lpOverlapped)
                except KeyError:
                    # Looks like the actual completion got here before this
                    # fallback post did -- we're in the "expected" case of
                    # too-late-to-cancel, where the user did nothing wrong.
                    # Nothing more to do.
                    pass
                else:
                    exc = _core.TrioInternalError(
                        "Failed to cancel overlapped I/O in {} and didn't "
                        "receive the completion either. Did you forget to "
                        "call register_with_iocp()?".format(waiter.name)
                    )
                    # Raising this out of handle_io ensures that
                    # the user will see our message even if some
                    # other task is in an uncancellable wait due
                    # to the same underlying forgot-to-register
                    # issue (if their CancelIoEx succeeds, we
                    # have no way of noticing that their completion
                    # won't arrive). Unfortunately it loses the
                    # task traceback. If you're debugging this
                    # error and can't tell where it's coming from,
                    # try changing this line to
                    # _core.reschedule(waiter, outcome.Error(exc))
                    raise exc
            else:
                # dispatch on lpCompletionKey
                queue = self._completion_key_queues[entry.lpCompletionKey]
                overlapped = int(ffi.cast("uintptr_t", entry.lpOverlapped))
                transferred = entry.dwNumberOfBytesTransferred
                info = CompletionKeyEventInfo(
                    lpOverlapped=overlapped,
                    dwNumberOfBytesTransferred=transferred,
                )
                queue.put_nowait(info)

    def _register_with_iocp(self, handle, completion_key):
        handle = _handle(handle)
        _check(
            kernel32.CreateIoCompletionPort(
                handle, self._iocp, completion_key, 0
            )
        )
        # Supposedly this makes things slightly faster, by disabling the
        # ability to do WaitForSingleObject(handle). We would never want to do
        # that anyway, so might as well get the extra speed (if any).
        # Ref: http://www.lenholgate.com/blog/2009/09/interesting-blog-posts-on-high-performance-servers.html
        _check(
            kernel32.SetFileCompletionNotificationModes(
                handle, CompletionModes.FILE_SKIP_SET_EVENT_ON_HANDLE
            )
        )

    ################################################################
    # AFD stuff
    ################################################################

    def _refresh_afd(self, base_handle):
        waiters = self._afd_waiters[base_handle]
        if waiters.current_op is not None:
            try:
                _check(
                    kernel32.CancelIoEx(
                        self._afd, waiters.current_op.lpOverlapped
                    )
                )
            except OSError as exc:
                if exc.winerror != ErrorCodes.ERROR_NOT_FOUND:
                    # I don't think this is possible, so if it happens let's
                    # crash noisily.
                    raise  # pragma: no cover
            waiters.current_op = None

        flags = 0
        if waiters.read_task is not None:
            flags |= READABLE_FLAGS
        if waiters.write_task is not None:
            flags |= WRITABLE_FLAGS

        if not flags:
            del self._afd_waiters[base_handle]
        else:
            lpOverlapped = ffi.new("LPOVERLAPPED")

            poll_info = ffi.new("AFD_POLL_INFO *")
            poll_info.Timeout = 2**63 - 1  # INT64_MAX
            poll_info.NumberOfHandles = 1
            poll_info.Exclusive = 0
            poll_info.Handles[0].Handle = base_handle
            poll_info.Handles[0].Status = 0
            poll_info.Handles[0].Events = flags

            try:
                _check(
                    kernel32.DeviceIoControl(
                        self._afd,
                        IoControlCodes.IOCTL_AFD_POLL,
                        poll_info,
                        ffi.sizeof("AFD_POLL_INFO"),
                        poll_info,
                        ffi.sizeof("AFD_POLL_INFO"),
                        ffi.NULL,
                        lpOverlapped,
                    )
                )
            except OSError as exc:
                if exc.winerror != ErrorCodes.ERROR_IO_PENDING:
                    # This could happen if the socket handle got closed behind
                    # our back while a wait_* call was pending, and we tried
                    # to re-issue the call. Clear our state and wake up any
                    # pending calls.
                    del self._afd_waiters[base_handle]
                    # Do this last, because it could raise.
                    wake_all(waiters, exc)
                    return
            op = AFDPollOp(lpOverlapped, poll_info, waiters)
            waiters.current_op = op
            self._afd_ops[lpOverlapped] = op

    async def _afd_poll(self, sock, mode):
        base_handle = _get_base_socket(sock)
        waiters = self._afd_waiters.get(base_handle)
        if waiters is None:
            waiters = AFDWaiters()
            self._afd_waiters[base_handle] = waiters
        if getattr(waiters, mode) is not None:
            raise _core.BusyResourceError
        setattr(waiters, mode, _core.current_task())
        # Could potentially raise if the handle is somehow invalid; that's OK,
        # we let it escape.
        self._refresh_afd(base_handle)

        def abort_fn(_):
            setattr(waiters, mode, None)
            self._refresh_afd(base_handle)
            return _core.Abort.SUCCEEDED

        await _core.wait_task_rescheduled(abort_fn)

    @_public
    async def wait_readable(self, sock):
        await self._afd_poll(sock, "read_task")

    @_public
    async def wait_writable(self, sock):
        await self._afd_poll(sock, "write_task")

    @_public
    def notify_closing(self, handle):
        handle = _get_base_socket(handle)
        waiters = self._afd_waiters.get(handle)
        if waiters is not None:
            wake_all(waiters, _core.ClosedResourceError())
            self._refresh_afd(handle)

    ################################################################
    # Regular overlapped operations
    ################################################################

    @_public
    def register_with_iocp(self, handle):
        self._register_with_iocp(handle, CKeys.WAIT_OVERLAPPED)

    @_public
    async def wait_overlapped(self, handle, lpOverlapped):
        handle = _handle(handle)
        if isinstance(lpOverlapped, int):
            lpOverlapped = ffi.cast("LPOVERLAPPED", lpOverlapped)
        if lpOverlapped in self._overlapped_waiters:
            raise _core.BusyResourceError(
                "another task is already waiting on that lpOverlapped"
            )
        task = _core.current_task()
        self._overlapped_waiters[lpOverlapped] = task
        raise_cancel = None

        def abort(raise_cancel_):
            nonlocal raise_cancel
            raise_cancel = raise_cancel_
            try:
                _check(kernel32.CancelIoEx(handle, lpOverlapped))
            except OSError as exc:
                if exc.winerror == ErrorCodes.ERROR_NOT_FOUND:
                    # Too late to cancel. If this happens because the
                    # operation is already completed, we don't need to do
                    # anything; we'll get a notification of that completion
                    # soon. But another possibility is that the operation was
                    # performed on a handle that wasn't registered with our
                    # IOCP (ie, the user forgot to call register_with_iocp),
                    # in which case we're just never going to see the
                    # completion. To avoid an uncancellable infinite sleep in
                    # the latter case, we'll PostQueuedCompletionStatus here,
                    # and if our post arrives before the original completion
                    # does, we'll assume the handle wasn't registered.
                    _check(
                        kernel32.PostQueuedCompletionStatus(
                            self._iocp, 0, CKeys.LATE_CANCEL, lpOverlapped
                        )
                    )
                    # Keep the lpOverlapped referenced so its address
                    # doesn't get reused until our posted completion
                    # status has been processed. Otherwise, we can
                    # get confused about which completion goes with
                    # which I/O.
                    self._posted_too_late_to_cancel.add(lpOverlapped)
                else:  # pragma: no cover
                    raise _core.TrioInternalError(
                        "CancelIoEx failed with unexpected error"
                    ) from exc
            return _core.Abort.FAILED

        await _core.wait_task_rescheduled(abort)
        if lpOverlapped.Internal != 0:
            # the lpOverlapped reports the error as an NT status code,
            # which we must convert back to a Win32 error code before
            # it will produce the right sorts of exceptions
            code = ntdll.RtlNtStatusToDosError(lpOverlapped.Internal)
            if code == ErrorCodes.ERROR_OPERATION_ABORTED:
                if raise_cancel is not None:
                    raise_cancel()
                else:
                    # We didn't request this cancellation, so assume
                    # it happened due to the underlying handle being
                    # closed before the operation could complete.
                    raise _core.ClosedResourceError(
                        "another task closed this resource"
                    )
            else:
                raise_winerror(code)

    async def _perform_overlapped(self, handle, submit_fn):
        # submit_fn(lpOverlapped) submits some I/O
        # it may raise an OSError with ERROR_IO_PENDING
        # the handle must already be registered using
        # register_with_iocp(handle)
        # This always does a schedule point, but it's possible that the
        # operation will not be cancellable, depending on how Windows is
        # feeling today. So we need to check for cancellation manually.
        await _core.checkpoint_if_cancelled()
        lpOverlapped = ffi.new("LPOVERLAPPED")
        try:
            submit_fn(lpOverlapped)
        except OSError as exc:
            if exc.winerror != ErrorCodes.ERROR_IO_PENDING:
                raise
        await self.wait_overlapped(handle, lpOverlapped)
        return lpOverlapped

    @_public
    async def write_overlapped(self, handle, data, file_offset=0):
        with ffi.from_buffer(data) as cbuf:

            def submit_write(lpOverlapped):
                # yes, these are the real documented names
                offset_fields = lpOverlapped.DUMMYUNIONNAME.DUMMYSTRUCTNAME
                offset_fields.Offset = file_offset & 0xffffffff
                offset_fields.OffsetHigh = file_offset >> 32
                _check(
                    kernel32.WriteFile(
                        _handle(handle),
                        ffi.cast("LPCVOID", cbuf),
                        len(cbuf),
                        ffi.NULL,
                        lpOverlapped,
                    )
                )

            lpOverlapped = await self._perform_overlapped(handle, submit_write)
            # this is "number of bytes transferred"
            return lpOverlapped.InternalHigh

    @_public
    async def readinto_overlapped(self, handle, buffer, file_offset=0):
        with ffi.from_buffer(buffer, require_writable=True) as cbuf:

            def submit_read(lpOverlapped):
                offset_fields = lpOverlapped.DUMMYUNIONNAME.DUMMYSTRUCTNAME
                offset_fields.Offset = file_offset & 0xffffffff
                offset_fields.OffsetHigh = file_offset >> 32
                _check(
                    kernel32.ReadFile(
                        _handle(handle),
                        ffi.cast("LPVOID", cbuf),
                        len(cbuf),
                        ffi.NULL,
                        lpOverlapped,
                    )
                )

            lpOverlapped = await self._perform_overlapped(handle, submit_read)
            return lpOverlapped.InternalHigh

    ################################################################
    # Raw IOCP operations
    ################################################################

    @_public
    def current_iocp(self):
        return int(ffi.cast("uintptr_t", self._iocp))

    @contextmanager
    @_public
    def monitor_completion_key(self):
        key = next(self._completion_key_counter)
        queue = _core.UnboundedQueue()
        self._completion_key_queues[key] = queue
        try:
            yield (key, queue)
        finally:
            del self._completion_key_queues[key]
