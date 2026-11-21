"""Lifetime exclusion for web servers sharing a canonical ledger.

This is separate from LedgerGuard: a server must NOT hold the domain lock while
waiting for HTTP, a human, or a model. Separate open descriptions also reject a
second app in this process. The stable sidecar is never unlinked.
"""

import fcntl
import os
from pathlib import Path


class WebInstanceBusyError(RuntimeError):
    """Another web instance owns the dataset."""


class WebInstanceLock:
    def __init__(self, changes_path: Path) -> None:
        ledger = changes_path.resolve()
        self.path = ledger.with_name(ledger.name + ".web.lock")
        self._fd: int | None = None

    def __enter__(self):
        if self._fd is not None:
            raise WebInstanceBusyError("This web instance is already running.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise WebInstanceBusyError(
                "Another web instance already owns this dataset."
            ) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc_info: object) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
