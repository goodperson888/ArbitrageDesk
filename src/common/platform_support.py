"""Platform boundaries for the source tree and portable Windows distribution."""
import getpass
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys


def execution_python(root):
    root = Path(root)
    if sys.platform == 'win32':
        portable = root / 'python/python.exe'
        return portable if portable.is_file() else root / '.venv-execution/Scripts/python.exe'
    return root / '.venv-execution/bin/python'


def user_scope():
    if hasattr(os, 'getuid'):
        return str(os.getuid())
    return hashlib.sha256(getpass.getuser().encode('utf-8')).hexdigest()[:16]


def private_fchmod(fd, mode=0o600):
    # Windows uses the containing user's inherited ACL, not POSIX mode bits.
    if hasattr(os, 'fchmod'):
        os.fchmod(fd, mode)


def open_private_read(path):
    """Reject links/replacements also on systems without O_NOFOLLOW."""
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError(errno.ELOOP, 'Private file must be a regular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('Private file changed while opening')
        return fd
    except BaseException:
        os.close(fd)
        raise


def process_alive(pid):
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # os.kill(pid, 0) can terminate a process on Windows. Query a handle instead.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        # Access denied is inconclusive; never claim an inaccessible worker exited.
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)
