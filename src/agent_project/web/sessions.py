"""Bounded, process-local browser sessions; never serialize these as HTTP DTOs."""

import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

SESSION_COOKIE = "expense_session"
CSRF_HEADER = "x-csrf-token"


@dataclass
class BrowserSession:
    session_id: str
    csrf_token: str
    last_seen: float


class SessionCapacityError(RuntimeError):
    """The bounded session store is full; do not evict active owners."""


class SessionStore:
    """Eight-hour idle TTL, with a hard bound and an injectable monotonic clock."""

    def __init__(
        self,
        *,
        ttl: float = 8 * 60 * 60,
        capacity: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or ttl <= 0
            or isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity < 1
        ):
            raise ValueError("Session limits must be positive.")
        self._ttl = ttl
        self._capacity = capacity
        self._clock = clock
        self._sessions: dict[str, BrowserSession] = {}
        self._mutex = threading.Lock()

    def _lookup(self, session_id: str | None, now: float) -> BrowserSession | None:
        session = self._sessions.get(session_id) if session_id else None
        if session is not None and now - session.last_seen >= self._ttl:
            del self._sessions[session.session_id]
            session = None
        if session is not None:
            session.last_seen = now
        return session

    def get(self, session_id: str | None) -> BrowserSession | None:
        with self._mutex:
            return self._lookup(session_id, self._clock())

    def bootstrap(self, session_id: str | None) -> tuple[BrowserSession, bool]:
        with self._mutex:
            now = self._clock()
            for key, value in list(self._sessions.items()):
                if now - value.last_seen >= self._ttl:
                    del self._sessions[key]
            existing = self._lookup(session_id, now)
            if existing is not None:
                return existing, False
            if len(self._sessions) >= self._capacity:
                raise SessionCapacityError("Session capacity is exhausted.")
            session = BrowserSession(
                secrets.token_urlsafe(32), secrets.token_urlsafe(32), now
            )
            self._sessions[session.session_id] = session
            return session, True
