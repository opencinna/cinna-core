"""
Local Agent Kit — the public, unauthenticated ``/agent-start`` surface.

A user with any local coding assistant and *no account* pastes one prompt
("read https://<instance>/agent-start and help me start making my agents"). The
assistant fetches this surface and receives a versioned, platform-maintained
kit: how to lay out ``~/Documents/MyAgents/{Local,Cloud}``, how to build a local
agent whose folder layout and metadata are byte-compatible with a cloud agent
workspace, a capability ladder, and the go-cloud playbook.

Source of truth
---------------
The kit is hand-authored in ``docs/local_agent_kit/`` and snapshotted into the
``platform-knowledge-env`` template by ``sync_platform_knowledge.py`` (step 3).
``docs/`` is not shipped in the backend image, so that snapshot
(``platform_knowledge_assets.local_kit_dir()``) is the only copy available at
runtime. A missing snapshot is a build defect: it fails loud with 503 rather
than serving an empty kit.

Rendering
---------
Instance-specific values reach the kit as ``{{TOKEN}}`` placeholders resolved
here by **plain string substitution** — not a template engine. The kit is full
of fenced shell and JSON with braces, and a real engine would either choke on it
or force the content to be written defensively. Only the fixed token set below
is substituted; anything else (notably the lowercase ``{{name}}`` / ``{{slug}}``
scaffold placeholders that ``kit.py new`` fills in on the user's machine) is
left verbatim.

Every placeholder value comes from **settings**, never from the request. The
``Host`` header is deliberately never read: the go-cloud guide tells the user
which host to run ``cinna login`` against, and reflecting an attacker-supplied
Host there would point that login at them.

Versioning
----------
``kit_version`` is a content hash (sha256, first 16 hex chars) over the sorted
``(path, rendered bytes)`` pairs, computed with ``{{KIT_VERSION}}`` still
*unsubstituted* — the version cannot depend on itself. It is substituted
afterwards, which fills both the ``VERSION`` member and ``kit.json``'s
``kit_version`` field. ``kit.py refresh`` compares the local ``VERSION`` with
``GET /api/agent-start/version`` to decide whether to re-download.

The rendered tree and its tarball are built once per snapshot and memoized (key:
``snapshot_cache_key``, the same mtime+count probe ``ContextPackageService``
uses). Requests are served from the in-memory tree only — the filesystem is
never touched per-request, which makes path traversal impossible by
construction rather than by validation.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import logging
import tarfile
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlmodel import Session

from app.core.config import settings
from app.services.cli.platform_knowledge_assets import (
    local_kit_dir,
    snapshot_cache_key,
)

logger = logging.getLogger(__name__)

# The kit's own entry document, and the tarball's root directory. The tarball is
# rooted rather than flat so extracting it anywhere yields one obvious folder;
# ``kit.py`` moves it to ``<root>/.cinna-kit/``.
START_MEMBER = "START.md"
INDEX_MEMBER = "kit.json"
VERSION_MEMBER = "VERSION"
TARBALL_ROOT = "cinna-kit"
TARBALL_FILENAME = "cinna-kit.tar.gz"

KIT_VERSION_HEADER = "X-Kit-Version"

# The kit is text. A rendered tree past this size means the snapshot picked up
# something it should not have (a checked-in binary, a stray venv) — a build
# defect worth failing on rather than streaming to every anonymous caller.
MAX_RENDERED_BYTES = 5 * 1024 * 1024

# Directory names never read out of the snapshot.
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}

# The placeholder token the content version is computed *without*.
_VERSION_TOKEN = "{{KIT_VERSION}}"

# Content-Security-Policy for the HTML landing page only. The page is
# self-contained: no images, no fonts, no XHR, one inline stylesheet and one
# inline copy-button script with no dynamic content.
HTML_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"

# Media types by file extension. Everything textual is served as text so a
# browser or a curl-to-terminal assistant renders it instead of downloading it.
_MEDIA_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".toml": "text/plain; charset=utf-8",
    ".example": "text/plain; charset=utf-8",
    ".cfg": "text/plain; charset=utf-8",
    ".ini": "text/plain; charset=utf-8",
    ".sh": "text/plain; charset=utf-8",
}
_TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"
_DEFAULT_MEDIA_TYPE = "application/octet-stream"


def instance_display_name(raw: str) -> str:
    """Operator-facing instance name for the public kit.

    ``PROJECT_NAME`` reaches the container through docker-compose ``env_file``,
    which passes a ``PROJECT_NAME="Cinna"`` line through with its quotes intact.
    Inside the platform that only shows up in email headers; on an anonymous
    page it reads as a typo. Strip ONE matching pair of surrounding quotes and
    outer whitespace — nothing else, so an inner quote survives untouched.
    """
    name = raw.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ('"', "'"):
        name = name[1:-1].strip()
    return name or raw


class LocalAgentKitService:
    """Builds, renders, versions and caches the Local Agent Kit."""

    # (cache_key, kit_version, rendered tree, tarball). Process-local, rebuilt
    # when the snapshot changes on disk (i.e. on redeploy).
    _cache: tuple[str, str, dict[str, bytes], bytes] | None = None
    _lock = threading.Lock()

    # ── Instance toggle ──────────────────────────────────────────────────

    @staticmethod
    def is_enabled(session: Session) -> bool:
        """Whether this instance publishes the kit at all.

        Backed by ``ServerConfig.local_agent_kit_enabled``. ``getattr`` with a
        default of ``True`` because the column arrives in a later phase: an
        instance whose schema predates it publishes the kit, which is the
        column's own default.
        """
        from app.services.server_config.server_config_service import (
            ServerConfigService,
        )

        config = ServerConfigService.get_or_create(session)
        return bool(getattr(config, "local_agent_kit_enabled", True))

    # ── Placeholders ─────────────────────────────────────────────────────

    @staticmethod
    def placeholders() -> dict[str, str]:
        """Instance values substituted into the kit. Settings only, never the request.

        ``KIT_VERSION`` is deliberately absent — it is resolved after hashing by
        :meth:`_build_or_cached`.
        """
        frontend = settings.FRONTEND_HOST.rstrip("/")
        backend = settings.backend_base_url
        return {
            "PLATFORM_URL": frontend,
            # Always the /api/ alias: it is proxied by the universal /api/ block
            # on every deployment, whereas the pretty /agent-start URL needs its own
            # nginx location. Kit-internal links must work either way.
            "KIT_BASE_URL": f"{backend}/api/agent-start",
            "START_URL": f"{frontend}/agent-start",
            "SIGNUP_URL": f"{frontend}/signup",
            "LOGIN_URL": f"{frontend}/login",
            "INSTANCE_NAME": instance_display_name(settings.PROJECT_NAME),
            "CLI_INSTALL_SPEC": settings.CINNA_CLI_INSTALL_SPEC,
            "MIN_CLI_VERSION": settings.MINIMUM_CLI_VERSION,
        }

    # ── Public API ───────────────────────────────────────────────────────

    @classmethod
    def get_version(cls) -> str:
        """The current kit's content version."""
        return cls._build_or_cached()[0]

    @classmethod
    def get_version_payload(cls) -> dict[str, Any]:
        """Body of ``GET /agent-start/version`` — enough for ``kit.py refresh``."""
        version = cls.get_version()
        values = cls.placeholders()
        return {
            "kit_version": version,
            "schema_version": 1,
            "platform_url": values["PLATFORM_URL"],
            "kit_base_url": values["KIT_BASE_URL"],
            "start_url": values["START_URL"],
            "instance_name": values["INSTANCE_NAME"],
            "cli": {
                "install_spec": values["CLI_INSTALL_SPEC"],
                "min_version": values["MIN_CLI_VERSION"],
            },
        }

    @classmethod
    def get_versioned_file(
        cls, rel_path: str
    ) -> tuple[str, tuple[bytes, str] | None]:
        """Return ``(kit_version, (content, media_type) | None)`` in one build.

        Lookup is an exact dict hit against the in-memory rendered tree, so
        ``..`` segments, absolute paths and symlinks cannot resolve to anything:
        they simply are not keys.

        The version comes back alongside the content because every caller needs
        both, and each independent call re-walks the snapshot to compute the
        cache key — cheap once, wasteful three times on the hot path of an
        anonymous surface.
        """
        version, rendered, _ = cls._build_or_cached()
        normalized = cls._normalize_rel_path(rel_path)
        if normalized is None:
            return version, None
        content = rendered.get(normalized)
        if content is None:
            return version, None
        return version, (content, cls.media_type_for(normalized))

    @classmethod
    def get_file(cls, rel_path: str) -> tuple[bytes, str] | None:
        """``(content, media_type)`` for a kit-relative path, or ``None``."""
        return cls.get_versioned_file(rel_path)[1]

    @classmethod
    def get_start_markdown(cls) -> str:
        """The rendered ``START.md``."""
        _, rendered, _ = cls._build_or_cached()
        content = rendered.get(START_MEMBER)
        if content is None:
            logger.error(
                "Local agent kit snapshot has no %s (snapshot at %s)",
                START_MEMBER,
                local_kit_dir(),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Local agent kit is not available on this instance",
            )
        return content.decode("utf-8")

    @classmethod
    def get_start_html(cls) -> str:
        """A self-contained HTML landing page wrapping the full ``START.md``.

        The complete markdown is embedded verbatim (escaped, inside ``<pre>``),
        so a browser reader and an assistant that followed a mis-negotiated
        ``Accept`` header both end up with the same instructions.
        """
        markdown = cls.get_start_markdown()
        values = cls.placeholders()
        instance = html.escape(values["INSTANCE_NAME"])
        start_url = html.escape(values["START_URL"])
        prompt = f"read {values['START_URL']} and help me start making my agents"
        prompt_escaped = html.escape(prompt)
        return _LANDING_TEMPLATE.format(
            instance=instance,
            start_url=start_url,
            prompt=prompt_escaped,
            version=html.escape(cls.get_version()),
            markdown=html.escape(markdown),
        )

    @classmethod
    def get_rendered_tree(cls) -> tuple[str, dict[str, bytes]]:
        """``(kit_version, {relative path: rendered bytes})`` — the served kit.

        The rendered tree, not the raw snapshot: a consumer that re-packaged the
        files off disk would ship `{{KIT_BASE_URL}}` and friends unresolved, so
        the kit would describe no instance at all. The account context package
        embeds this tree under ``context/local-kit/``.
        """
        version, rendered, _ = cls._build_or_cached()
        return version, rendered

    @classmethod
    def get_tarball(cls) -> bytes:
        """The whole rendered kit as a gzip tarball rooted at ``cinna-kit/``."""
        return cls._build_or_cached()[2]

    @classmethod
    def get_versioned_tarball(cls) -> tuple[str, bytes]:
        """``(kit_version, tarball)`` from one build, for the download route."""
        version, _, tarball = cls._build_or_cached()
        return version, tarball

    @staticmethod
    def media_type_for(rel_path: str) -> str:
        """Media type for a kit-relative path."""
        name = rel_path.rsplit("/", 1)[-1]
        suffix = Path(name).suffix.lower()
        if suffix in _MEDIA_TYPES:
            return _MEDIA_TYPES[suffix]
        if not suffix:
            # Extension-less (``Makefile``, ``VERSION``) and bare dotfiles
            # (``.gitignore`` has no suffix once it is the whole name) are text.
            return _TEXT_MEDIA_TYPE
        return _DEFAULT_MEDIA_TYPE

    # ── Build / cache ────────────────────────────────────────────────────

    @classmethod
    def _build_or_cached(cls) -> tuple[str, dict[str, bytes], bytes]:
        """Return ``(kit_version, rendered tree, tarball)``, building if stale."""
        kit_dir = local_kit_dir()
        cache_key = snapshot_cache_key(kit_dir)

        cached = cls._cache
        if cached is not None and cached[0] == cache_key:
            return cached[1], cached[2], cached[3]

        with cls._lock:
            # Re-check inside the lock: another thread may have just built it.
            cached = cls._cache
            if cached is not None and cached[0] == cache_key:
                return cached[1], cached[2], cached[3]

            version, rendered = cls._render_tree(kit_dir)
            tarball = cls._build_tarball(rendered)
            cls._cache = (cache_key, version, rendered, tarball)
            logger.info(
                "Built local agent kit (%d files, %d tarball bytes, "
                "kit_version=%s, cache_key=%s)",
                len(rendered),
                len(tarball),
                version,
                cache_key,
            )
            return version, rendered, tarball

    @classmethod
    def _render_tree(cls, kit_dir: Path) -> tuple[str, dict[str, bytes]]:
        """Read the snapshot, substitute placeholders, and hash the result."""
        raw = cls._read_snapshot(kit_dir)

        values = cls.placeholders()
        # Render everything except the version, which cannot be known yet.
        staged = {
            rel: cls._render_bytes(content, values, json_mode=cls._is_json(rel))
            for rel, content in raw.items()
        }

        version = cls._content_version(staged)

        # ``version`` is hex, so it needs no JSON escaping in either mode.
        rendered = {
            rel: content.replace(_VERSION_TOKEN.encode("utf-8"), version.encode("utf-8"))
            for rel, content in staged.items()
        }
        return version, rendered

    @staticmethod
    def _read_snapshot(kit_dir: Path) -> dict[str, bytes]:
        """Read every regular file under the snapshot. Fails loud when absent."""
        if not kit_dir.is_dir():
            logger.error(
                "Local agent kit snapshot missing at %s — the image was built "
                "without `make sync-platform-knowledge`",
                kit_dir,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Local agent kit is not available on this instance",
            )

        raw: dict[str, bytes] = {}
        total = 0
        for path in sorted(kit_dir.rglob("*")):
            rel = path.relative_to(kit_dir)
            if _SKIP_DIRS & set(rel.parts):
                continue
            # A symlink is never followed: the snapshot is a flat copy, and a
            # link is either broken or an escape attempt.
            if path.is_symlink() or not path.is_file():
                continue
            # Size is checked before the read, not after: the cap exists for
            # exactly the case (a stray large file in the snapshot) that would
            # otherwise exhaust the worker's memory on the way to the check.
            total += path.stat().st_size
            if total > MAX_RENDERED_BYTES:
                logger.error(
                    "Local agent kit snapshot at %s exceeds %d bytes — refusing "
                    "to load it (the kit is text; this is a build defect)",
                    kit_dir,
                    MAX_RENDERED_BYTES,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Local agent kit is not available on this instance",
                )
            raw[rel.as_posix()] = path.read_bytes()

        if not raw:
            logger.error("Local agent kit snapshot at %s is empty", kit_dir)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Local agent kit is not available on this instance",
            )
        return raw

    @staticmethod
    def _is_json(rel_path: str) -> bool:
        """Whether a kit member is JSON, and so needs escaped substitution."""
        return rel_path.lower().endswith(".json")

    @staticmethod
    def _render_bytes(
        content: bytes, values: dict[str, str], *, json_mode: bool = False
    ) -> bytes:
        """Substitute the fixed placeholder set in UTF-8 text; pass bytes through.

        Decodability is the text test rather than an extension allowlist: the kit
        is asserted to be all-text, and a stray binary is passed through
        untouched instead of raising.

        ``json_mode`` escapes each value the way a JSON string literal needs it.
        Every placeholder inside the kit's ``.json`` members sits between quotes,
        and a value may legitimately contain one — ``PROJECT_NAME`` is operator
        text — so a raw splice would emit a file that no parser accepts. This is
        the one place the substitution is not byte-dumb, and it is contextual
        escaping, not templating.
        """
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return content
        for token, value in values.items():
            if json_mode:
                # json.dumps quotes and escapes; strip the outer quotes since the
                # kit file already supplies them.
                value = json.dumps(value)[1:-1]
            text = text.replace("{{" + token + "}}", value)
        return text.encode("utf-8")

    @staticmethod
    def _content_version(staged: dict[str, bytes]) -> str:
        """sha256 over the sorted ``(path, bytes)`` pairs, first 16 hex chars.

        Computed on the *pre-version* render so the hash cannot depend on
        itself, and independent of mtimes so a redeploy shipping byte-identical
        content does not tell every kit on disk that it is behind.
        """
        digest = hashlib.sha256()
        for rel in sorted(staged):
            digest.update(f"{rel}\0".encode("utf-8"))
            digest.update(hashlib.sha256(staged[rel]).digest())
        return digest.hexdigest()[:16]

    @staticmethod
    def _build_tarball(rendered: dict[str, bytes]) -> bytes:
        """Pack the rendered tree into a gzip tarball rooted at ``cinna-kit/``."""
        buffer = io.BytesIO()
        # Fixed member mtimes and a zeroed gzip header: the same rendered tree
        # must produce the same bytes in every worker and on every rebuild.
        # Wall-clock timestamps would make two workers serve different bodies
        # under one strong ETag, which is precisely what a validator promises
        # cannot happen.
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for rel in sorted(rendered):
                    content = rendered[rel]
                    info = tarfile.TarInfo(name=f"{TARBALL_ROOT}/{rel}")
                    info.size = len(content)
                    info.mtime = 0
                    info.mode = 0o755 if rel.endswith(".py") else 0o644
                    info.type = tarfile.REGTYPE
                    tar.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    # ── Path handling ────────────────────────────────────────────────────

    @staticmethod
    def _normalize_rel_path(rel_path: str) -> str | None:
        """Normalize a requested kit path, or ``None`` if it is not addressable.

        Rejects absolute paths, ``.``/``..`` segments, empty segments and NUL
        bytes. Lookup is a dict hit either way, so this is defence in depth (and
        keeps the 404 cheap) rather than the only barrier.
        """
        if not rel_path or "\x00" in rel_path:
            return None
        candidate = rel_path.replace("\\", "/").strip("/")
        if not candidate:
            return None
        parts = candidate.split("/")
        for part in parts:
            if part in ("", ".", ".."):
                return None
        return "/".join(parts)


# The landing page. Deliberately one self-contained document: no SPA assets, no
# external fonts, no network of any kind (see HTML_CSP). ``{markdown}`` carries
# the complete escaped START.md, so nothing a reader needs is only in the chrome.
_LANDING_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{instance} — build agents locally</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0 auto; padding: 2rem 1.25rem; max-width: 52rem; line-height: 1.5;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .25rem; }}
  p.lead {{ margin: 0 0 1.5rem; opacity: .8; }}
  .prompt {{
    display: flex; gap: .5rem; align-items: stretch; margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }}
  .prompt code {{
    flex: 1 1 20rem; padding: .75rem 1rem; border-radius: .5rem;
    border: 1px solid currentColor; font-size: .95rem; overflow-wrap: anywhere;
  }}
  button {{
    padding: .75rem 1rem; border-radius: .5rem; border: 1px solid currentColor;
    background: transparent; color: inherit; font: inherit; cursor: pointer;
  }}
  pre {{
    padding: 1rem; border-radius: .5rem; border: 1px solid currentColor;
    overflow-x: auto; white-space: pre-wrap; word-wrap: break-word;
    font-size: .875rem;
  }}
  footer {{ margin-top: 2rem; font-size: .8125rem; opacity: .7; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
<h1>Build agents on your own machine</h1>
<p class="lead">
  This page is written for a coding assistant. Machine-readable markdown:
  <a href="?format=md">{start_url}?format=md</a>. No account is needed to start.
</p>
<div class="prompt">
  <code id="kit-prompt">{prompt}</code>
  <button type="button" id="kit-copy" aria-label="Copy the starter prompt">Copy</button>
</div>
<pre>{markdown}</pre>
<footer>Kit version {version}</footer>
<script>
  document.getElementById('kit-copy').addEventListener('click', function () {{
    var text = document.getElementById('kit-prompt').textContent;
    var button = this;
    navigator.clipboard.writeText(text).then(function () {{
      button.textContent = 'Copied';
      setTimeout(function () {{ button.textContent = 'Copy'; }}, 2000);
    }});
  }});
</script>
</body>
</html>
"""
