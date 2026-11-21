"""Same-origin ASGI boundary with bounded JSON buffering for unsafe methods."""

import json
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_project.web.presentation import http_error
from agent_project.web.sessions import CSRF_HEADER, SESSION_COOKIE

SECURITY_HEADERS = {
    "cache-control": "no-store",
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'; object-src 'none'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}
# Allows 8000 non-BMP characters even as 12-byte JSON surrogate escapes.
MAX_REQUEST_BYTES = 128 * 1024


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _header(scope: Scope, name: str) -> str | None:
    """Ambiguous repeated security headers fail closed, not last-value-wins."""
    values = [
        value for key, value in scope["headers"] if key.lower() == name.encode("ascii")
    ]
    return values[0].decode("latin-1") if len(values) == 1 else None


class LocalSecurityMiddleware:
    def __init__(self, app: ASGIApp, *, authority: str) -> None:
        self.app = app
        self.authority = authority
        self.origin = f"http://{authority}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def secured_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                protected = {key.encode() for key in SECURITY_HEADERS}
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in protected
                    and not key.lower().startswith(b"access-control-")
                ]
                headers.extend(
                    (key.encode(), value.encode())
                    for key, value in SECURITY_HEADERS.items()
                )
                message = {**message, "headers": headers}
            await send(message)

        async def reject(code="forbidden"):
            status, body = http_error(code)
            await JSONResponse(body.model_dump(), status_code=status)(
                scope, receive, secured_send
            )

        if scope.get("scheme") != "http" or _header(scope, "host") != self.authority:
            await reject()
            return
        origins = [value for key, value in scope["headers"] if key.lower() == b"origin"]
        origin = _header(scope, "origin")
        if origins and origin != self.origin:
            await reject()
            return
        unsafe = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        bootstrap = scope["path"] == "/api/session" and scope["method"] == "GET"
        fetch_site = _header(scope, "sec-fetch-site")
        if (unsafe or bootstrap) and fetch_site not in {None, "none", "same-origin"}:
            await reject()
            return
        request = Request(scope)
        cookies = [value for key, value in scope["headers"] if key.lower() == b"cookie"]
        if len(cookies) > 1 or (
            cookies
            and sum(
                part.strip().partition(b"=")[0] == SESSION_COOKIE.encode()
                for part in cookies[0].split(b";")
            )
            > 1
        ):
            await reject()
            return
        session = request.app.state.sessions.get(request.cookies.get(SESSION_COOKIE))
        if (scope["path"].startswith("/api/") and not bootstrap) or unsafe:
            if session is None:
                await reject()
                return
            scope.setdefault("state", {})["browser_session"] = session
        if unsafe:
            csrf = _header(scope, CSRF_HEADER)
            if (
                origin != self.origin
                or csrf is None
                or not secrets.compare_digest(
                    csrf.encode("latin-1"), session.csrf_token.encode("ascii")
                )
            ):
                await reject()
                return
            content_type = _header(scope, "content-type")
            if content_type is None or content_type.split(";", 1)[
                0
            ].strip().lower() != ("application/json"):
                await reject("invalid_request")
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    await reject("invalid_request")
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            try:
                payload = json.loads(
                    body.decode("utf-8"),
                    object_pairs_hook=_json_object,
                    parse_constant=_invalid_constant,
                )
            except (ValueError, UnicodeError, RecursionError):
                await reject("invalid_request")
                return
            if not isinstance(payload, dict) or not isinstance(
                payload.get("instance_id"), str
            ):
                await reject("invalid_request")
                return
            if payload["instance_id"] != request.app.state.instance_id:
                await reject("instance_changed")
                return
            delivered = False
            original_receive = receive

            async def replay_body():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await original_receive()

            receive = replay_body
        try:
            await self.app(scope, receive, secured_send)
        except Exception:
            if started:
                raise
            await reject("internal_error")
