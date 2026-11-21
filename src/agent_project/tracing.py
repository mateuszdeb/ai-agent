"""Small JSON Lines trace helper with basic secret redaction."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

_SECRET_FIELDS = {"api_key", "authorization", "openrouter_api_key", "token"}


class TraceRecord(TypedDict):
    """Internal event snapshot, not a public HTTP DTO."""

    timestamp: str
    event: str
    payload: dict[str, Any]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SECRET_FIELDS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def make_trace_record(event: str, payload: dict[str, Any]) -> TraceRecord:
    """Build one timestamped, redacted snapshot for trace and observers."""
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        "payload": _redact(payload),
    }


def append_trace_record(path: Path, record: TraceRecord) -> None:
    """Append an already prepared record without rebuilding its timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_trace(path: Path, event: str, payload: dict[str, Any]) -> None:
    """Backward-compatible helper for callers outside the agent loop."""
    append_trace_record(path, make_trace_record(event, payload))
