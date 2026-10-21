# -*- coding: utf-8 -*-
"""按 SQLite 文件隔离的巡检进程锁；进程退出后由操作系统自动释放。"""
from __future__ import annotations

import errno
import os
import threading

from core import db


class InspectionLease:
    def __init__(self, handle):
        self._handle = handle
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def acquire_inspection_lease() -> InspectionLease | None:
    db._ensure_sqlite()
    path = db._active_sqlite_path().resolve().with_suffix('.inspection.lock')
    handle = path.open('a+b')
    try:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            return None
        raise
    except BaseException:
        handle.close()
        raise
    return InspectionLease(handle)
