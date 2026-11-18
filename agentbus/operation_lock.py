"""Per-task OS locks; Codex integration, 2026-10-08.

The lock has no TTL: the kernel releases it when its owning process exits.
Use the same key for claim, submit, finish, block, heartbeat and recovery.
Lock files are permanent identities; never unlink a live lock file.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import os
from pathlib import Path
import threading
import time

_registry_guard = threading.Lock()
_registry = {}
_held = threading.local()


@contextmanager
def task_lock(bus_root, agent, task_id, *, timeout_s=10):
    root = Path(bus_root).resolve()
    key = hashlib.sha256((str(agent) + '\0' + str(task_id)).encode('utf-8')).hexdigest()
    directory = root / 'operation-locks'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (key + '.lock')
    if directory.is_symlink() or path.is_symlink() or not directory.resolve().is_relative_to(root):
        raise OSError('operation lock path is unsafe')
    identity = str(path)
    with _registry_guard:
        local_lock = _registry.setdefault(identity, threading.RLock())
    deadline = time.monotonic() + timeout_s
    if not local_lock.acquire(timeout=max(0, timeout_s)):
        raise TimeoutError('task operation is busy; local wait exceeded its deadline')
    try:
        held = getattr(_held, 'keys', None)
        if held is None:
            _held.keys = held = set()
        if identity in held:
            yield
            return
        with path.open('a+b') as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'\0')
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('task operation is busy; retry after its process releases the lock')
                    time.sleep(.02)
            held.add(identity)
            try:
                yield
            finally:
                held.remove(identity)
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        local_lock.release()


def serialized_task(method):
    @wraps(method)
    def wrapped(bus, agent, item, *args, **kwargs):
        task_id = item.id if hasattr(item, 'id') else item.stem if isinstance(item, Path) else item
        with task_lock(bus.root, agent, task_id):
            return method(bus, agent, item, *args, **kwargs)
    return wrapped
