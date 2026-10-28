"""Bounded diagnostic logs and conservative cleanup of owned program caches."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import threading
import time

LOG_LIMIT = 1024 * 1024
LOG_BACKUPS = 2
VERSION_FOLDER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)-[0-9a-f]{16}\Z")
TEMP_FILE = re.compile(r"SongdoDaily\.\d+\.tmp\Z")


def process_alive(pid):
    """Check a stale server marker without terminating or signalling its owner."""
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # Invalid PID; access denied stays protected.
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


def is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def cleanup_versions(root, protected):
    """Remove only known cache files, never links, unknown files, or business data.

    Call while holding the updater lock, after a successful activation. Protect
    active/previous builds and every currently executing program.
    """
    root = Path(root)
    keep = {Path(path).parent.name for path in protected if path}
    removed = []
    try:
        if is_link(root):
            return removed
        boundary = root.absolute()
        for folder in root.iterdir():
            if folder.name in keep or not VERSION_FOLDER.fullmatch(folder.name) or is_link(folder) or not folder.is_dir():
                continue
            # Check the final absolute directory before any deletion on Windows.
            # A Windows projected folder can resolve outside its logical parent
            # even though it is not a junction/link. Check the exact direct child
            # returned by this root and reject links above before deleting only
            # the allowlisted leaf files (no recursive directory removal).
            absolute = folder.absolute()
            if absolute.parent != boundary:
                continue
            try:
                files = list(folder.iterdir())
                if any(is_link(path) or not path.is_file() or
                       (path.name != "SongdoDaily.exe" and not TEMP_FILE.fullmatch(path.name)) for path in files):
                    continue
                for path in files:
                    path.unlink()
                folder.rmdir()
                removed.append(folder.name)
            except OSError:
                pass  # Windows keeps in-use EXEs locked; try again next launch.
    except OSError:
        pass
    return removed


@contextmanager
def log_lock(path):
    """Short cross-process lock; diagnostic failures must not stop the app."""
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + .25
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Diagnostic log is busy")
                time.sleep(.01)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BoundedLog:
    """Text stream with short-lived file handles, safe for windowless children."""
    encoding = "utf-8"

    def __init__(self, path, *, limit=LOG_LIMIT, backups=LOG_BACKUPS):
        self.path = Path(path)
        self.limit, self.backups = limit, backups
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write("")  # Also bound an oversized log left by an older program.

    def write(self, value):
        data = value.encode(self.encoding, errors="replace")[-self.limit:].decode(self.encoding, errors="ignore").encode(self.encoding)
        if not data:
            try:
                if not self.path.exists() or self.path.stat().st_size <= self.limit:
                    return len(value)
            except OSError:
                return len(value)
        try:
            with self.lock, log_lock(self.path.with_name(self.path.name + ".lock")):
                if self.path.exists() and self.path.stat().st_size + len(data) > self.limit:
                    try:
                        for index in range(self.backups, 0, -1):
                            source = self.path if index == 1 else self.path.with_name(self.path.name + f".{index - 1}")
                            if source.exists():
                                if source.stat().st_size > self.limit:
                                    with source.open("rb") as old:
                                        old.seek(-self.limit, 2)
                                        tail = old.read().decode(self.encoding, errors="ignore").encode(self.encoding)
                                    source.write_bytes(tail)
                                source.replace(self.path.with_name(self.path.name + f".{index}"))
                    except OSError:
                        # An older launcher can still hold the log open on Windows.
                        # Keep the size bounded even when renaming is unavailable.
                        self.path.write_bytes(b"")
                with self.path.open("ab") as output:
                    output.write(data)
        except OSError:
            pass
        return len(value)

    def flush(self):
        pass

    def isatty(self):
        return False
