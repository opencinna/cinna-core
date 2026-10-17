"""Backend-side body cap for artifact uploads, independent of the proxy.

nginx caps ``/api/v1/(cli/account/)?artifacts`` bodies at 55 MiB, but a
deployment without that block (or a direct hit on the backend) would let a
multipart body of any size spool to disk before the ingest's own limits run.
This wrapper refuses, for those paths, any body larger than
``ARTIFACT_MAX_REVISION_BYTES + ARTIFACT_UPLOAD_OVERHEAD_BYTES``: up front from
``Content-Length``, and while streaming for chunked bodies (raised as an
``HTTPException`` from ``receive``, which FastAPI re-raises out of form parsing).
"""
import json
import re

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

_UPLOAD_PATH = re.compile(rf"^{re.escape(settings.API_V1_STR)}/(cli/account/)?artifacts(/|$)")
_METHODS = frozenset({"POST", "PUT", "PATCH"})


def max_upload_bytes() -> int:
    return settings.ARTIFACT_MAX_REVISION_BYTES + settings.ARTIFACT_UPLOAD_OVERHEAD_BYTES


def _too_large_detail(limit: int) -> str:
    return f"Upload body is larger than {limit} bytes"


class ArtifactUploadLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in _METHODS
            or not _UPLOAD_PATH.match(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        limit = max_upload_bytes()
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                too_large = False
            if too_large:
                await self._reject(send, limit)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail=_too_large_detail(limit))
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        body = json.dumps({"detail": _too_large_detail(limit)}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
