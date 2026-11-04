"""Serving dashboard component pages: ``/d/v/<cap>/<path>`` (D-C1, D-C2).

One sandboxed frame per tile, served by URL from a capability path on the
artifacts content origin. Per request: verify the cap, re-check that its user
still owns the block or agent, refuse unsafe paths, and read the file from the
agent's *running* environment through env-core's ``/dashboards/file``. A
suspended environment is never woken here (the frame mint already woke it):
the request answers 503 until it runs.

The component's entry page gets the kit's reference bridge
(``protocols/dashboards-bridge.js``) injected inline, byte for byte, before the
page's own scripts. The bridge file is never altered.

Every failure is reported by tile id only; a page may carry data in its path
or query, so neither is ever logged (D-C3).
"""
import logging
import mimetypes
import posixpath
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.users.user_dashboard import UserDashboard, UserDashboardBlock
from app.services.artifacts.artifact_content import EXPOSED_HEADERS, frame_ancestor
from app.services.dashboards import frame_cap
from app.services.dashboards.component_service import (
    AGENT_COMPONENT_VIEW_TYPE,
    ComponentService,
    DashboardComponentError,
)
from app.services.dashboards.frame_cap import FrameCapClaims
from app.services.environments.platform_protocol_files import kit_snapshot_dir

logger = logging.getLogger(__name__)

BRIDGE_RELATIVE_PATH = "protocols/dashboards-bridge.js"

_SANDBOX = "sandbox allow-scripts allow-forms allow-modals"
_HEAD_TAG = re.compile(rb"<head(?:\s[^>]*)?>", re.IGNORECASE)
_SCRIPT_TAG = re.compile(rb"<script[\s>]", re.IGNORECASE)
_HTML_TAG = re.compile(rb"<html(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)
# An HTML comment, or an unterminated one running to the end of the file.
_COMMENT = re.compile(rb"<!--.*?(?:-->|\Z)", re.DOTALL)
_BOM = b"\xef\xbb\xbf"


# ── Headers ─────────────────────────────────────────────────────────


def content_csp(src: str, ancestor: str) -> str:
    """The frame CSP of plan §4, every source scoped to ``src`` (``<origin>/d/v/<cap>/``)."""
    return (
        f"{_SANDBOX}; "
        "default-src 'none'; "
        f"connect-src {src}; "
        f"script-src {src} 'unsafe-inline'; "
        f"style-src {src} 'unsafe-inline'; "
        f"img-src {src} data: blob:; "
        f"font-src {src} data:; "
        f"media-src {src} blob:; "
        "form-action 'none'; "
        "base-uri 'none'; "
        "frame-src 'none'; "
        "worker-src 'none'; "
        f"frame-ancestors {ancestor}"
    )


def error_csp(ancestor: str) -> str:
    """CSP for error responses: the same sandbox, nothing may load."""
    return (
        f"{_SANDBOX}; default-src 'none'; form-action 'none'; base-uri 'none'; "
        f"frame-ancestors {ancestor}"
    )


def _common_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": EXPOSED_HEADERS,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


def build_headers(cap: str) -> dict[str, str]:
    """Headers for a served file (``cap`` must be a verified cap)."""
    return {
        "Content-Security-Policy": content_csp(frame_cap.content_base(cap), frame_ancestor()),
        **_common_headers(),
    }


def error_headers() -> dict[str, str]:
    """Headers for any non-success ``/d/`` response."""
    return {"Content-Security-Policy": error_csp(frame_ancestor()), **_common_headers()}


# ── Bridge injection ────────────────────────────────────────────────


def bridge_path() -> Path:
    return kit_snapshot_dir(Path(settings.ENV_TEMPLATES_DIR)) / BRIDGE_RELATIVE_PATH


def read_bridge() -> bytes:
    """The exact bytes of the kit's reference bridge (raises ``OSError`` when missing)."""
    return bridge_path().read_bytes()


def _insertion_point(html: bytes) -> int:
    """Where the bridge goes so it runs before any of the page's scripts.

    Tags inside ``<!--…-->`` are ignored. Right after ``<head…>`` when it comes
    before the first ``<script`` (a ``"<head>"`` string inside a script is not
    a head); otherwise before the first ``<script``; with no script, after
    ``<html…>`` or the doctype, else at the very start (after a UTF-8 BOM).
    """
    # Same length as ``html``, comments blanked, so offsets carry over.
    masked = _COMMENT.sub(lambda m: b" " * len(m.group(0)), html)
    head = _HEAD_TAG.search(masked)
    script = _SCRIPT_TAG.search(masked)
    if head and (script is None or head.start() < script.start()):
        return head.end()
    if script:
        return script.start()
    for pattern in (_HTML_TAG, _DOCTYPE):
        match = pattern.search(masked)
        if match:
            return match.end()
    return len(_BOM) if html.startswith(_BOM) else 0


def inject_bridge(html: bytes, bridge: bytes) -> bytes:
    """``html`` with ``<script>`` + ``bridge`` + ``</script>`` inserted (bridge bytes unaltered)."""
    at = _insertion_point(html)
    return html[:at] + b"<script>" + bridge + b"</script>" + html[at:]


# ── Paths ───────────────────────────────────────────────────────────


def is_safe_path(path: str) -> bool:
    """A component-relative file path: no ``api/``, dot segments, leading ``/`` or backslash."""
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    segments = path.split("/")
    if segments[0] == "api":
        return False
    return all(segment not in ("", ".", "..") for segment in segments)


# ── Resolution ──────────────────────────────────────────────────────


class FrameContentError(Exception):
    """A refusal; the route answers ``status`` with the error headers."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _not_found() -> FrameContentError:
    return FrameContentError(404, "Not found")


@dataclass(frozen=True)
class FrameTile:
    """The owner-checked tile a cap resolves to."""

    tile_id: str
    agent: Agent
    component: str
    skill: str | None


@dataclass(frozen=True)
class FrameFile:
    body: bytes
    media_type: str
    headers: dict[str, str] = field(default_factory=dict)


def tile_id(claims: FrameCapClaims) -> str:
    """The id errors are logged by: the block id, or ``agent/skill/component[/tile_key]``."""
    target = claims.target
    if target.block_id is not None:
        return str(target.block_id)
    parts = [str(target.agent_id), target.skill or "-", target.component or ""]
    if target.tile_key:
        parts.append(target.tile_key)
    return "/".join(parts)


def _resolve_tile(session: Session, claims: FrameCapClaims) -> FrameTile:
    """Re-check ownership of the cap's target; 404 when the user no longer owns it."""
    target = claims.target
    label = tile_id(claims)
    if target.block_id is not None:
        block = session.get(UserDashboardBlock, target.block_id)
        dashboard = session.get(UserDashboard, block.dashboard_id) if block else None
        if (
            block is None
            or dashboard is None
            or dashboard.owner_id != claims.user_id
            or block.view_type != AGENT_COMPONENT_VIEW_TYPE
            or block.agent_id is None
        ):
            raise _not_found()
        agent = session.get(Agent, block.agent_id)
        config = block.config if isinstance(block.config, dict) else {}
        component = str(config.get("component") or "")
        if agent is None or not component:
            raise _not_found()
        return FrameTile(label, agent, component, config.get("skill") or None)
    agent = session.get(Agent, target.agent_id) if target.agent_id else None
    if agent is None or agent.owner_id != claims.user_id or not target.component:
        raise _not_found()
    return FrameTile(label, agent, target.component, target.skill)


def _media_type(path: str, reported: str | None) -> str:
    if reported:
        return reported
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


class FrameContentService:
    @staticmethod
    async def serve(session: Session, cap: str, path: str, *, is_head: bool = False) -> FrameFile:
        """The file at ``path`` under ``cap``, entry page bridged; ``FrameContentError`` otherwise.

        Only a GET of the entry page counts as activity (a HEAD never keeps the
        environment awake).
        """
        claims = frame_cap.verify(cap)
        if claims is None:
            raise _not_found()
        tile = _resolve_tile(session, claims)
        if not is_safe_path(path):
            raise _not_found()

        try:
            environment, adapter = await ComponentService.running_environment(
                session, tile.agent, activate=False
            )
            components = await ComponentService.list_in_environment(environment, adapter)
        except DashboardComponentError as e:
            if e.code == "env_unavailable":
                raise FrameContentError(503, "The environment is not running.")
            logger.warning("Dashboard frame for tile %s: listing failed (%s)", tile.tile_id, e.code)
            raise FrameContentError(502, "The environment did not answer.")
        entry = ComponentService.find(components, tile.component, tile.skill)
        if entry is None:
            raise _not_found()
        try:
            entry_page = ComponentService.entry_page(entry)
        except DashboardComponentError:
            raise _not_found()

        try:
            status, content_type, body = await adapter.read_dashboard_file(
                tile.component, tile.skill, path
            )
        except Exception:
            logger.warning("Dashboard frame for tile %s: the file read did not reach the environment", tile.tile_id)
            raise FrameContentError(502, "The environment did not answer.")
        if status == 404:
            raise _not_found()
        if status >= 400:
            logger.warning("Dashboard frame for tile %s: the environment answered %s", tile.tile_id, status)
            raise FrameContentError(502, "The file could not be read.")

        media_type = _media_type(path, content_type)
        if path == posixpath.normpath(entry_page).lstrip("/"):
            try:
                bridge = read_bridge()
            except OSError:
                logger.error("Dashboard frame for tile %s: the kit bridge is missing", tile.tile_id)
                raise FrameContentError(503, "The dashboard bridge is unavailable.")
            body = inject_bridge(body, bridge)
            if not is_head:
                ComponentService.touch_activity(session, environment)
        return FrameFile(body=body, media_type=media_type, headers=build_headers(cap))
