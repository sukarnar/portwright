"""Pure-ASGI middleware: request IDs, security headers, request-size limit."""
from __future__ import annotations

import json
import re
import uuid

_SAFE_ID = re.compile(r"^[A-Za-z0-9\-_.]{1,64}$")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
]
# Swagger UI (bundled locally) needs scripts/styles from this origin and one inline bootstrap script.
DOCS_CSP = (b"content-security-policy",
            b"default-src 'self'; script-src 'self' 'unsafe-inline'; "
            b"style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")


async def _send_json(send, status: int, body: dict, headers: list | None = None) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode())] + (headers or [])})
    await send({"type": "http.response.body", "body": payload})


class SecurityMiddleware:
    def __init__(self, app, max_body_bytes: int, hsts: bool = False, docs_paths: tuple[str, ...] = ()):
        self.app = app
        self.max_body = max_body_bytes
        self.hsts = hsts
        self.docs_paths = docs_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode(errors="ignore")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        rid_header = (b"x-request-id", request_id.encode())

        # Fast reject on declared length
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > self.max_body:
            return await _send_json(send, 413, {"error": "Request body too large",
                                                "request_id": request_id}, [rid_header])

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise _BodyTooLarge()
            return message

        is_docs = scope.get("path", "").startswith(self.docs_paths) if self.docs_paths else False

        async def secure_send(message):
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                existing = {k.lower() for k, _ in hdrs}
                extra = [DOCS_CSP if (is_docs and k == b"content-security-policy") else (k, v)
                         for k, v in SECURITY_HEADERS]
                hdrs += [h for h in extra if h[0] not in existing]
                hdrs.append(rid_header)
                if self.hsts:
                    hdrs.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = hdrs
            await send(message)

        try:
            await self.app(scope, limited_receive, secure_send)
        except _BodyTooLarge:
            await _send_json(send, 413, {"error": "Request body too large",
                                         "request_id": request_id}, [rid_header])


class _BodyTooLarge(Exception):
    pass
