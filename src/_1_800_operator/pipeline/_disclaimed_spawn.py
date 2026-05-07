"""posix_spawn wrapper that disclaims TCC responsibility for the child.

Why this exists: macOS attributes TCC checks (Screen Recording, Microphone)
to the closest user-launched app in a process's responsibility chain — the
"responsible process". When operator-audio-capture is launched as a normal
subprocess from Python, its responsible process is whichever IDE/terminal
the user started operator from (Cursor, iTerm, etc.). SCStream's audio
gate checks against the responsible process, which on Cursor's
ToDesktop-wrapped Electron build silently denies even when the helper
itself has been granted Screen Recording.

Apple's fix: posix_spawn() with `responsibility_spawnattrs_setdisclaim(&attrs, 1)`
makes the spawned child its own responsible process — TCC then keys
decisions against the child's own code-signature identifier
(`com.operator.audio-capture`), regardless of who launched it.

The API is private (no public headers), but it's a stable symbol in
libSystem and is what BackgroundMusic, Yabai, and similar tools use to
escape weird responsibility chains. Stable since macOS 10.14.

Returns a minimal subprocess.Popen-shaped object — just the methods
AttachAdapter actually uses (pid, stdin, stdout, poll, wait, terminate,
kill). Not a Popen subclass: avoids inheriting Popen's posix_spawn path
which doesn't expose the disclaim attr.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from typing import Sequence

# libSystem exposes both posix_spawn* and responsibility_* via the default
# ctypes namespace on macOS. CDLL(None) → process-default symbols.
_libc = ctypes.CDLL(None, use_errno=True)

# posix_spawnattr_t and posix_spawn_file_actions_t are opaque handles —
# typedef'd to void* on macOS. The library allocates the underlying struct
# in *_init and frees it in *_destroy; we hold a c_void_p.

_posix_spawnattr_init = _libc.posix_spawnattr_init
_posix_spawnattr_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_posix_spawnattr_init.restype = ctypes.c_int

_posix_spawnattr_destroy = _libc.posix_spawnattr_destroy
_posix_spawnattr_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_posix_spawnattr_destroy.restype = ctypes.c_int

_posix_spawnattr_setflags = _libc.posix_spawnattr_setflags
_posix_spawnattr_setflags.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_short]
_posix_spawnattr_setflags.restype = ctypes.c_int

_posix_spawn_file_actions_init = _libc.posix_spawn_file_actions_init
_posix_spawn_file_actions_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_posix_spawn_file_actions_init.restype = ctypes.c_int

_posix_spawn_file_actions_destroy = _libc.posix_spawn_file_actions_destroy
_posix_spawn_file_actions_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_posix_spawn_file_actions_destroy.restype = ctypes.c_int

_posix_spawn_file_actions_addclose = _libc.posix_spawn_file_actions_addclose
_posix_spawn_file_actions_addclose.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
_posix_spawn_file_actions_addclose.restype = ctypes.c_int

_posix_spawn_file_actions_adddup2 = _libc.posix_spawn_file_actions_adddup2
_posix_spawn_file_actions_adddup2.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_int,
]
_posix_spawn_file_actions_adddup2.restype = ctypes.c_int

# private API
_responsibility_spawnattrs_setdisclaim = _libc.responsibility_spawnattrs_setdisclaim
_responsibility_spawnattrs_setdisclaim.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
_responsibility_spawnattrs_setdisclaim.restype = ctypes.c_int

_posix_spawn = _libc.posix_spawn
_posix_spawn.argtypes = [
    ctypes.POINTER(ctypes.c_int),                  # pid out
    ctypes.c_char_p,                               # path
    ctypes.POINTER(ctypes.c_void_p),               # file_actions
    ctypes.POINTER(ctypes.c_void_p),               # attrp
    ctypes.POINTER(ctypes.c_char_p),               # argv
    ctypes.POINTER(ctypes.c_char_p),               # envp
]
_posix_spawn.restype = ctypes.c_int


def _check(rc: int, what: str) -> None:
    if rc != 0:
        raise OSError(rc, f"{what}: {os.strerror(rc)}")


def _argv_array(args: Sequence[str]) -> ctypes.Array:
    encoded = [a.encode() for a in args] + [None]
    arr = (ctypes.c_char_p * len(encoded))()
    for i, a in enumerate(encoded):
        arr[i] = a
    return arr


def _envp_array() -> ctypes.Array:
    items = [f"{k}={v}".encode() for k, v in os.environ.items()] + [None]
    arr = (ctypes.c_char_p * len(items))()
    for i, a in enumerate(items):
        arr[i] = a
    return arr


class DisclaimedProcess:
    """Minimal subprocess.Popen-shaped wrapper for a disclaim-spawned child.

    Implements only the subset AttachAdapter touches: .pid, .stdin, .stdout,
    .poll(), .wait(timeout=), .terminate(), .kill(). Stderr is inherited
    from a caller-supplied fd (matching how Popen treats a passed-in file
    object) — we don't pipe it.
    """

    def __init__(self, pid: int, stdin_fd: int, stdout_fd: int):
        self.pid = pid
        # bufsize=0 unbuffered, matches AttachAdapter's existing Popen call.
        self.stdin = os.fdopen(stdin_fd, "wb", buffering=0)
        self.stdout = os.fdopen(stdout_fd, "rb", buffering=0)
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._returncode = -1
            return self._returncode
        if pid == 0:
            return None
        self._returncode = _exit_code(status)
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is not None:
            return self._returncode
        if timeout is None:
            _, status = os.waitpid(self.pid, 0)
            self._returncode = _exit_code(status)
            return self._returncode
        # Poll loop — posix_spawn'd processes can't use waitpid timeout
        # natively, and threads avoid SIGCHLD complications.
        import time as _time
        deadline = _time.monotonic() + timeout
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if _time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args_for_error, timeout)
            _time.sleep(0.05)

    @property
    def args_for_error(self) -> str:
        return "<disclaimed-spawn>"

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, sig: int) -> None:
        if self._returncode is not None:
            return
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            self._returncode = -1


def _exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return -1


def spawn_disclaimed(
    args: Sequence[str],
    stderr_fd: int | None = None,
) -> DisclaimedProcess:
    """posix_spawn args[0] with TCC responsibility disclaimed.

    Sets up pipes for stdin/stdout (matching subprocess.PIPE) and inherits
    stderr from `stderr_fd` (or 2 if None — caller's stderr). Raises OSError
    on any spawn failure with a meaningful errno.

    The returned DisclaimedProcess has subprocess.Popen-compatible
    .pid/.stdin/.stdout/.poll/.wait/.terminate/.kill. The child runs as its
    own responsible process — TCC checks key against args[0]'s code-signed
    identifier, not the launcher's responsibility chain.
    """
    if not args:
        raise ValueError("args must be non-empty")

    # Pipes for stdin / stdout. parent_*: ends parent keeps. child_*: ends
    # child keeps (closed in parent after spawn).
    stdin_r, stdin_w = os.pipe()  # child reads stdin_r; parent writes stdin_w
    try:
        stdout_r, stdout_w = os.pipe()  # child writes stdout_w; parent reads stdout_r
    except OSError:
        # Don't leak the first pipe if the second os.pipe() hits the fd limit.
        os.close(stdin_r)
        os.close(stdin_w)
        raise

    file_actions = ctypes.c_void_p()
    attrs = ctypes.c_void_p()
    spawn_failed = True
    pid_out = ctypes.c_int(0)

    try:
        _check(_posix_spawn_file_actions_init(ctypes.byref(file_actions)), "file_actions_init")
        # Child fd plumbing: dup pipe ends to 0/1, close the originals + the
        # parent-side ends (child shouldn't hold parent's write/read ends).
        _check(_posix_spawn_file_actions_adddup2(ctypes.byref(file_actions), stdin_r, 0), "dup2 stdin")
        _check(_posix_spawn_file_actions_adddup2(ctypes.byref(file_actions), stdout_w, 1), "dup2 stdout")
        if stderr_fd is not None and stderr_fd != 2:
            _check(_posix_spawn_file_actions_adddup2(ctypes.byref(file_actions), stderr_fd, 2), "dup2 stderr")
        _check(_posix_spawn_file_actions_addclose(ctypes.byref(file_actions), stdin_r), "close child stdin_r")
        _check(_posix_spawn_file_actions_addclose(ctypes.byref(file_actions), stdin_w), "close child stdin_w")
        _check(_posix_spawn_file_actions_addclose(ctypes.byref(file_actions), stdout_r), "close child stdout_r")
        _check(_posix_spawn_file_actions_addclose(ctypes.byref(file_actions), stdout_w), "close child stdout_w")

        _check(_posix_spawnattr_init(ctypes.byref(attrs)), "spawnattr_init")
        # POSIX_SPAWN_CLOEXEC_DEFAULT (0x4000) closes ALL fds in the child
        # except those explicitly preserved via file_actions. Tested with this
        # flag set and the helper produced ZERO stderr — likely something in
        # dyld / Foundation needs an fd we didn't enumerate. Without the
        # flag, leaked Python fds (logs, sockets) inherit but are CLOEXEC
        # by default in modern Python anyway, so the leak is bounded.
        # The actual disclaim — this is the whole reason the wrapper exists.
        _check(_responsibility_spawnattrs_setdisclaim(ctypes.byref(attrs), 1), "setdisclaim")

        argv = _argv_array(args)
        envp = _envp_array()
        rc = _posix_spawn(
            ctypes.byref(pid_out),
            args[0].encode(),
            ctypes.byref(file_actions),
            ctypes.byref(attrs),
            argv,
            envp,
        )
        if rc != 0:
            raise OSError(rc, f"posix_spawn: {os.strerror(rc)}")
        spawn_failed = False
    finally:
        # Always destroy the attr/action objects (they were init'd above).
        try:
            _posix_spawn_file_actions_destroy(ctypes.byref(file_actions))
        except Exception:
            pass
        try:
            _posix_spawnattr_destroy(ctypes.byref(attrs))
        except Exception:
            pass
        # Parent closes child-only ends — must happen even on spawn failure
        # to avoid leaked fds.
        os.close(stdin_r)
        os.close(stdout_w)
        if spawn_failed:
            os.close(stdin_w)
            os.close(stdout_r)

    return DisclaimedProcess(pid=pid_out.value, stdin_fd=stdin_w, stdout_fd=stdout_r)
