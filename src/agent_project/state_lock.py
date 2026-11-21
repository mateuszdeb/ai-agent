"""Reentrant thread/process exclusion for a ledger on macOS and Linux.

All instances for a canonical ledger path share a thread RLock and one flock
descriptor. Never unlink the stable sidecar: doing so would split the lock into
different inodes. Locks protect local cooperating processes, not manual edits,
network filesystems or hostile replacement of files.
"""

from __future__ import annotations

import fcntl
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakValueDictionary

DEFAULT_LOCK_TIMEOUT = 2.0
_POLL_INTERVAL = 0.01


class LedgerBusyError(TimeoutError):
    """The shared ledger could not be locked within the time budget."""


class LedgerCorruptionError(ValueError):
    """The ledger cannot be replayed safely; do not skip or repair its events."""


class LedgerWriteError(OSError):
    """A write was started but its durable outcome is unknown. Never auto-retry."""


@dataclass
class _LockState:
    mutex: threading.RLock = field(default_factory=threading.RLock)
    pid: int = field(default_factory=os.getpid)
    depth: int = 0
    fd: int | None = None


_registry: WeakValueDictionary[Path, _LockState] = WeakValueDictionary()
_registry_mutex = threading.Lock()


def _state_for(path: Path) -> _LockState:
    with _registry_mutex:
        state = _registry.get(path)
        if state is None:
            state = _LockState()
            _registry[path] = state
        return state


def _after_fork() -> None:
    """Drop inherited descriptions without unlocking the parent's flock."""
    global _registry, _registry_mutex
    for state in list(_registry.values()):
        if state.fd is not None:
            os.close(state.fd)
            state.fd = None
    _registry = WeakValueDictionary()
    _registry_mutex = threading.Lock()


os.register_at_fork(after_in_child=_after_fork)


class LedgerGuard:
    """Lock one ledger; nesting separate instances in the same thread is safe.

    The timeout covers both thread and process contention using a monotonic
    deadline. A zero timeout is a nonblocking attempt. Locks are acquired only
    on entry, never on import or construction.
    """

    def __init__(self, changes_path: Path, *, timeout: float | None = None) -> None:
        timeout = DEFAULT_LOCK_TIMEOUT if timeout is None else timeout
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
            or timeout > threading.TIMEOUT_MAX
        ):
            raise ValueError("Lock timeout must be finite and non-negative.")
        self.timeout = float(timeout)
        self.changes_path = Path(changes_path).resolve()
        self.lock_path = self.changes_path.with_name(self.changes_path.name + ".lock")
        self._state = _state_for(self.changes_path)

    def __enter__(self) -> LedgerGuard:
        if self._state.pid != os.getpid():
            self._state = _state_for(self.changes_path)
        state = self._state
        deadline = time.monotonic() + self.timeout
        if not state.mutex.acquire(timeout=self.timeout):
            raise LedgerBusyError("Transaction state is busy.")
        try:
            if state.depth == 0:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise LedgerBusyError(
                                    "Transaction state is busy."
                                ) from None
                            time.sleep(min(_POLL_INTERVAL, remaining))
                except BaseException:
                    os.close(fd)
                    raise
                state.fd = fd
            state.depth += 1
        except BaseException:
            state.mutex.release()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        state = self._state
        if state.pid != os.getpid():
            raise RuntimeError("Cannot release a guard acquired before fork.")
        try:
            state.depth -= 1
            if state.depth == 0:
                fd, state.fd = state.fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            state.mutex.release()
