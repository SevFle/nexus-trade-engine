"""ASGI middleware that rejects oversized request bodies.

FastAPI / Starlette do not impose a default request-body cap. Without
one, an attacker can stream arbitrarily large payloads into memory
before Pydantic validation (or any other application logic) runs. The
per-route rate limiter does not help here: a single 100 MiB request
counts as one request, well within the cap.

This middleware:

- Short-circuits on declared ``Content-Length`` over the cap, returning
  ``413 Request Entity Too Large`` immediately so we never read the
  payload.
- Wraps the ASGI ``receive`` callable to count actual bytes received,
  in case the client lies about the length (chunked encoding, missing
  header, or proxy stripping). The first chunk that pushes the running
  total over the cap triggers a 413.

The cap is per-app-wide; routes that legitimately need larger uploads
(e.g. CSV imports) should be exempted by mounting them on a separate
app or by raising the cap at the proxy layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class BodySizeLimitExceededError(Exception):
    """Raised internally when the running byte total exceeds the cap.
    Caught at the middleware boundary and translated to a 413 response."""


class BodySizeLimitMiddleware:
    """Pure ASGI middleware (works alongside Starlette's BaseHTTPMiddleware)."""

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError(f"BodySizeLimitMiddleware.max_bytes must be > 0, got {max_bytes}")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast path: an honest Content-Length lets us reject before
        # reading a single byte off the wire.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > self.max_bytes:
                    await self._send_413(send)
                    return
                break

        # Wrap receive so chunked / lying clients are still capped.
        seen = 0
        cap = self.max_bytes

        async def _capped_receive() -> dict[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                seen += len(body)
                if seen > cap:
                    raise BodySizeLimitExceededError
            return message

        try:
            await self.app(scope, _capped_receive, send)
        except BodySizeLimitExceededError:
            # If the application has already started a response we
            # cannot rewrite headers; surface the failure to the
            # connection layer by re-raising. Otherwise emit a 413.
            await self._send_413(send)

    @staticmethod
    async def _send_413(
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        body = b'{"error":"request_body_too_large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["BodySizeLimitMiddleware"]
