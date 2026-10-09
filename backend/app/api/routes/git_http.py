"""Git smart HTTP for the team library (``/git/<repo_id>.git/...``).

Mounted at the app root, outside ``/api/v1`` and the OpenAPI schema:

    GET  /git/<repo_id>.git/info/refs?service=git-upload-pack|git-receive-pack
    POST /git/<repo_id>.git/git-upload-pack
    POST /git/<repo_id>.git/git-receive-pack

Auth: ``Authorization: Bearer <token>`` or Basic with the token as password
(``resolve_git_http_user``). Any auth failure → 401 with a Basic challenge;
unknown repo → 404; missing grant → 403 (``read`` for upload-pack, ``write`` for
receive-pack; mirrors are never writable). The database session is only held
while authorizing, never for the length of a transfer.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlmodel import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.deps import GitHttpAuthError, resolve_git_http_user
from app.core.db import engine
from app.models.library.git_repo import GitRepo
from app.services.library.exceptions import LibraryAccessDeniedError
from app.services.library.git_http_service import (
    RECEIVE_PACK,
    SERVICES,
    UPLOAD_PACK,
    GitHttpError,
    GitHttpService,
    ReceiveCommands,
    advertisement_content_type,
    result_content_type,
)
from app.services.library.git_repo_storage import GitRepoStorage
from app.services.library.library_events import LibraryEvents

router = APIRouter(prefix="/git", tags=["git"], include_in_schema=False)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
}


@dataclass
class _Authorized:
    repo_id: uuid.UUID
    repo_kind: str
    user_id: uuid.UUID


def _plain(status_code: int, message: str, headers: dict[str, str] | None = None) -> Response:
    return Response(
        content=f"{message}\n",
        status_code=status_code,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


def _unauthorized() -> Response:
    return _plain(
        401,
        "Sign in to Cinna to reach the team library.",
        {"WWW-Authenticate": 'Basic realm="cinna"'},
    )


class _Denied(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


def _authorize_sync(repo_file: str, service: str, authorization: str | None) -> _Authorized:
    repo_id = GitHttpService.parse_repo_file(repo_file)
    with Session(engine) as session:
        try:
            user = resolve_git_http_user(session, authorization)
        except GitHttpAuthError:
            raise _Denied(_unauthorized())
        repo = session.get(GitRepo, repo_id) if repo_id else None
        if repo is None or not GitRepoStorage.repo_path(repo.id).exists():
            raise _Denied(_plain(404, "Not found"))
        try:
            GitHttpService.authorize(session, repo, user, service)
        except LibraryAccessDeniedError as exc:
            raise _Denied(_plain(403, str(exc)))
        return _Authorized(repo_id=repo.id, repo_kind=repo.kind, user_id=user.id)


async def _authorize(request: Request, repo_file: str, service: str) -> _Authorized:
    return await run_in_threadpool(
        _authorize_sync, repo_file, service, request.headers.get("authorization")
    )


@router.get("/{repo_file}/info/refs")
async def git_info_refs(
    repo_file: str,
    request: Request,
    service: str = Query(default=""),
) -> Response:
    if service not in SERVICES:
        # The dumb protocol is not served.
        return _plain(403, "Only git smart HTTP is supported.")
    try:
        auth = await _authorize(request, repo_file, service)
    except _Denied as denied:
        return denied.response
    try:
        body = await GitHttpService.advertise(
            auth.repo_id,
            service,
            request.headers.get("git-protocol"),
            repo_kind=auth.repo_kind,
        )
    except GitHttpError as exc:
        return _plain(exc.status_code, str(exc))
    return Response(
        content=body,
        media_type=advertisement_content_type(service),
        headers=NO_CACHE_HEADERS,
    )


async def _rpc(request: Request, repo_file: str, service: str) -> Response:
    try:
        auth = await _authorize(request, repo_file, service)
    except _Denied as denied:
        return denied.response

    before_sha: str | None = None
    commands: ReceiveCommands | None = None
    if service == RECEIVE_PACK:
        before_sha = await run_in_threadpool(GitRepoStorage.head_sha, auth.repo_id)
        commands = ReceiveCommands()

    encoding = (request.headers.get("content-encoding") or "").strip().lower()
    stream = GitHttpService.run_rpc(
        auth.repo_id,
        service,
        request.stream(),
        gzip=encoding in ("gzip", "x-gzip"),
        git_protocol=request.headers.get("git-protocol"),
        repo_kind=auth.repo_kind,
        pusher_id=auth.user_id,
        commands=commands,
    )
    # Pull the first chunk before answering so a too-large body or a timeout
    # can still become a proper status code.
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = b""
    except GitHttpError as exc:
        await stream.aclose()
        return _plain(exc.status_code, str(exc))

    async def body() -> AsyncIterator[bytes]:
        try:
            if first:
                yield first
            async for chunk in stream:
                yield chunk
        except GitHttpError:
            return  # status already sent; git reports the truncated reply
        finally:
            await stream.aclose()

    background = None
    if commands is not None:
        background = BackgroundTask(
            _after_receive, auth.repo_id, before_sha, auth.user_id, commands
        )
    return StreamingResponse(
        body(),
        media_type=result_content_type(service),
        headers=NO_CACHE_HEADERS,
        background=background,
    )


async def _after_receive(
    repo_id: uuid.UUID,
    before_sha: str | None,
    pusher_id: uuid.UUID,
    commands: ReceiveCommands,
) -> None:
    """Bookkeeping for this push only: the SHA it asked main to become is read
    once the stream (and so the feeder) has finished."""
    await LibraryEvents.after_receive(repo_id, before_sha, pusher_id, commands.main_new)


@router.post("/{repo_file}/git-upload-pack")
async def git_upload_pack(repo_file: str, request: Request) -> Response:
    return await _rpc(request, repo_file, UPLOAD_PACK)


@router.post("/{repo_file}/git-receive-pack")
async def git_receive_pack(repo_file: str, request: Request) -> Response:
    return await _rpc(request, repo_file, RECEIVE_PACK)
