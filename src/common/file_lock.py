"""Exclusive cross-process file locks; keep native flock semantics on POSIX."""
import errno
import os
import time

if os.name != 'nt':
    from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
else:
    import msvcrt

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def flock(handle, operation):
        if operation not in (LOCK_EX, LOCK_EX | LOCK_NB, LOCK_UN):
            raise ValueError('Unsupported file lock operation')
        fd = handle.fileno()
        if operation == LOCK_UN:
            handle.seek(0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        # Windows byte locks need a stable byte; never replace/delete this file.
        if os.fstat(fd).st_size == 0:
            handle.write('\0')
            handle.flush()
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError(errno.EAGAIN, 'File is locked') from None
                time.sleep(.05)
