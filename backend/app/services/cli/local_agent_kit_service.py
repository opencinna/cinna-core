"""
Local Agent Kit — the public, unauthenticated ``/agent-start`` surface.

A user with any local coding assistant and *no account* pastes one prompt
("read https://<instance>/agent-start and help me start making my agents"). The
assistant fetches this surface and receives a versioned, platform-maintained
kit: how to lay out ``~/Documents/CinnaAgents/{Local,Cloud}``, how to build a local
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

The rendered tree and its two tarballs (the full kit, and the contract subset
described at ``CONTRACT_MEMBERS`` below) are built once per snapshot and
memoized (key: ``snapshot_cache_key``, the same mtime+count probe
``ContextPackageService`` uses). Requests are served from the in-memory tree
only — the filesystem is never touched per-request, which makes path traversal
impossible by construction rather than by validation.
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
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

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

# ── The contract ─────────────────────────────────────────────────────────
# The *contract* is the machine-readable half of the kit — the folder model,
# the manifest schema and the scaffold templates — that a non-kit host (Cinna
# Desktop) needs in order to create and validate agent folders that stay
# byte-compatible with ``kit.py``'s. It is a declared **subset of this one
# tree**, never a second tree: two trees would be two truths, and the drift
# would only surface when a desktop scaffold and a ``kit.py new`` scaffold
# stopped matching.
CONTRACT_VERSION_MEMBER = "CONTRACT_VERSION"
LAYOUT_MEMBER = "layout.json"
CHANGELOG_MEMBER = "CHANGELOG.md"
CONTRACT_TARBALL_ROOT = "cinna-contract"
CONTRACT_TARBALL_FILENAME = "cinna-contract.tar.gz"

# Exact member names in the contract, plus the directory prefixes that carry
# the rest of it. Everything else in the kit (the guides, ``tools/``,
# ``START.md``, ``README.md``, ``assistants/``) is prose for a human or a
# coding assistant and is deliberately not part of the contract.
#
# ``VERSION`` is deliberately absent: it holds the *kit* content version, and
# shipping it inside a contract tree would put two meanings on one filename.
# The contract's own version is ``CONTRACT_VERSION`` (and ``kit.json``'s
# ``contract_version``), which is hand-maintained rather than derived.
CONTRACT_MEMBERS = frozenset(
    {INDEX_MEMBER, LAYOUT_MEMBER, CONTRACT_VERSION_MEMBER, CHANGELOG_MEMBER}
)
CONTRACT_MEMBER_PREFIXES = ("schema/", "templates/")

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


class _KitBuild(NamedTuple):
    """Everything derived from one render of one snapshot.

    A record rather than a bare tuple because it grew a fifth field: positional
    unpacking with a row of underscores is how a later edit silently reads the
    contract tarball as the kit one.
    """

    version: str
    rendered: dict[str, bytes]
    tarball: bytes
    contract_tarball: bytes
    # ``None`` when the contract is serviceable, else the diagnostic naming why
    # it is not. Computed once per snapshot (see ``_build_or_cached``); it is
    # deliberately data here rather than an exception, so that carrying it
    # cannot 503 the kit surface — only the two contract accessors read it.
    contract_defect: str | None


class LocalAgentKitService:
    """Builds, renders, versions and caches the Local Agent Kit."""

    # ``(cache_key, build)``. Process-local, rebuilt when the snapshot changes
    # on disk (i.e. on redeploy). Everything derived from one render lives in
    # one entry behind one lock — both tarballs and the contract verdict —
    # because a second cache could hold a contract built from a different
    # render than the kit it claims to belong to, while reporting one
    # kit_version for both.
    _cache: tuple[str, _KitBuild] | None = None
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
        return cls._build_or_cached().version

    @classmethod
    def get_version_payload(cls) -> dict[str, Any]:
        """Body of ``GET /agent-start/version`` — enough for ``kit.py refresh``."""
        return cls._version_payload(cls.get_version())

    @classmethod
    def _version_payload(cls, version: str) -> dict[str, Any]:
        """The ``/version`` envelope for an already-resolved ``kit_version``.

        Split from :meth:`get_version_payload` so the contract payload can be
        built from the *same* build as its contract version instead of probing
        the snapshot a second time.

        There is deliberately **no** ``schema_version`` here. It used to be
        synthesised as a literal ``1`` mirroring ``kit.json``; both were removed
        together, because a second version number that decides nothing is the
        exact artefact this contract exists to eliminate. The compatibility gate
        is ``contract_version`` (``/contract/version``); the manifest's own
        ``schema_version`` is a different, still-live legacy marker and is not
        this. Do not re-add it "for parity with ``kit.json``" — ``kit.json`` no
        longer carries one either.
        """
        values = cls.placeholders()
        return {
            "kit_version": version,
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
    def get_contract_version(cls) -> str:
        """The hand-maintained contract version, from the ``CONTRACT_VERSION`` member."""
        build = cls._build_or_cached()
        cls._require_serviceable_contract(build)
        version = cls._read_contract_version(build.rendered)
        if version is None:  # unreachable: the gate above already rejected it
            cls._contract_defect("Local agent kit contract version vanished mid-build")
        return version

    @classmethod
    def get_contract_version_payload(cls) -> dict[str, Any]:
        """Body of ``GET /agent-start/contract/version``.

        The ``/version`` envelope plus ``contract_version``, so there is one
        payload builder and a consumer polling either endpoint parses the same
        shape.

        Both halves come out of **one** build: a second call would re-probe the
        snapshot (an ``rglob`` + a ``stat`` per file) on an anonymous endpoint,
        and — if a redeploy landed between the two probes — would pair one
        build's ``kit_version`` with another's ``contract_version``.
        """
        build = cls._build_or_cached()
        cls._require_serviceable_contract(build)
        contract_version = cls._read_contract_version(build.rendered)
        if contract_version is None:  # unreachable: the gate rejected it
            cls._contract_defect("Local agent kit contract version vanished mid-build")
        payload = cls._version_payload(build.version)
        payload["contract_version"] = contract_version
        return payload

    # ── Contract coherence (D16, amended) ────────────────────────────────

    @classmethod
    def _require_serviceable_contract(cls, build: _KitBuild) -> None:
        """503 unless this build's contract is coherent. The boundary is exact.

        **The anonymous kit surface — ``START.md``, ``/version``,
        ``kit.tar.gz``, ``/kit/{path}`` — never calls this.** It must keep
        serving a stranger whatever state the contract is in; that is the whole
        reason the verdict is carried as data rather than raised from
        :meth:`_build_or_cached`.

        **Both contract representations — ``/contract.tar.gz`` and
        ``/contract/version`` — always call it, and so degrade together.** Two
        representations of one thing reporting different health is the same
        asymmetry this guard exists to remove. ``/contract/version`` is the
        endpoint a consumer *polls*, and the number it publishes is what a
        major-version compatibility gate keys on: publishing an unvalidated one
        is worse than publishing nothing, because a false *compatibility*
        fails silently where a refusal at least stops.

        Do not narrow this to one representation, and do not widen it to the
        kit surface. Both directions have been tried and ruled on.
        """
        if build.contract_defect is not None:
            # Already logged with its specifics once per snapshot, at build.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Local agent kit is not available on this instance",
            )

    @classmethod
    def _contract_defect_reason(cls, rendered: dict[str, bytes]) -> str | None:
        """Why this rendered tree cannot serve a contract, or ``None`` if it can.

        Pure: it decides, it does not raise and does not log. Called once per
        snapshot from :meth:`_build_or_cached`; the two contract accessors turn
        its verdict into a 503 and the kit surface ignores it entirely.

        Two classes of defect, both of which would otherwise ship as a **200**
        carrying a quietly wrong archive or a quietly wrong number — the packer
        is a filter, not an assertion, so a member that is not in the tree is
        simply not packed.

        **1. The load-bearing member set: ``kit.json``, ``layout.json``,
        ``CONTRACT_VERSION``.** The first two are the consumer's own identity
        pair (Cinna Desktop's ``contractStore.isContractTree`` is exactly
        "``kit.json`` and ``layout.json`` at the root"), so an archive missing
        either is not a contract tree by the only definition that matters and
        there is nothing to gain by shipping it. The third is what D2 rests the
        version fallback on.

        ``schema/`` and ``templates/`` are deliberately **not** in this set: a
        thin or empty schema/templates tree is a *content* problem, and a
        content problem must not be dressed up as an identity failure.

        **2. The three declarations of ``contract_version`` must agree** —
        ``CONTRACT_VERSION``, ``kit.json``, ``layout.json``. A Phase 12
        invariant test only catches drift if someone runs it before shipping;
        checking here converts a silent inconsistency into a loud one at the
        one moment anyone can still act on it.
        """
        missing = sorted(
            member
            for member in (INDEX_MEMBER, LAYOUT_MEMBER, CONTRACT_VERSION_MEMBER)
            if member not in rendered
        )
        if missing:
            return f"missing {', '.join(missing)}"

        contract_version = cls._read_contract_version(rendered)
        if contract_version is None:
            return f"{CONTRACT_VERSION_MEMBER} is empty or undecodable"

        declared = {CONTRACT_VERSION_MEMBER: contract_version}
        for member in (INDEX_MEMBER, LAYOUT_MEMBER):
            try:
                document = json.loads(rendered[member])
            except (UnicodeDecodeError, ValueError):
                return f"unparseable {member}"
            value = (
                document.get("contract_version") if isinstance(document, dict) else None
            )
            if not isinstance(value, str) or not value.strip():
                return f"no usable contract_version in {member}"
            declared[member] = value.strip()

        if len(set(declared.values())) > 1:
            # All three are named, not just the disagreeing pair: it costs
            # nothing and saves the reader reconstructing the odd one out.
            values = ", ".join(f"{m}={v!r}" for m, v in declared.items())
            return f"contract_version disagrees across the snapshot: {values}"
        return None

    @staticmethod
    def _read_contract_version(rendered: dict[str, bytes]) -> str | None:
        """``CONTRACT_VERSION``, stripped — or ``None`` if it is unusable.

        Unlike ``kit_version`` this is **not** derived from the content: it is a
        literal file the kit authors bump when the folder model, the manifest
        schema or the templates change in a way a consuming host must know
        about. An absent, empty or undecodable member is a build defect (a
        snapshot that predates the contract, or a truncated sync) — a consumer
        cannot check compatibility against nothing, and an empty string would
        read as "compatible with anything".

        The decode is lenient because ``_render_bytes`` passes undecodable
        bytes through untouched: a corrupt member must land in the one
        "build defect ⇒ 503" mode, not in a 500.
        """
        raw = rendered.get(CONTRACT_VERSION_MEMBER)
        if raw is None:
            return None
        contract_version = raw.decode("utf-8", errors="replace").strip()
        # U+FFFD can only be the lenient decode's marker: the file is a
        # semver literal, so a replacement char means corrupt bytes.
        if not contract_version or "\ufffd" in contract_version:
            return None
        return contract_version

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
        build = cls._build_or_cached()
        version, rendered = build.version, build.rendered
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
        rendered = cls._build_or_cached().rendered
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
        build = cls._build_or_cached()
        return build.version, build.rendered

    @classmethod
    def get_tarball(cls) -> bytes:
        """The whole rendered kit as a gzip tarball rooted at ``cinna-kit/``."""
        return cls._build_or_cached().tarball

    @classmethod
    def get_versioned_tarball(cls) -> tuple[str, bytes]:
        """``(kit_version, tarball)`` from one build, for the download route."""
        build = cls._build_or_cached()
        return build.version, build.tarball

    @classmethod
    def get_versioned_contract_tarball(cls) -> tuple[str, bytes]:
        """``(kit_version, contract tarball)`` — the contract subset, rooted at
        ``cinna-contract/``.

        The version is the **kit** content version, not ``contract_version``:
        it is what the caching layer needs (see the route's ETag comment), and
        it is the version the contract was rendered as part of.

        Coherence is checked **here**, not in :meth:`_build_or_cached` and not
        in the route (D16). The kit surface — ``START.md``, ``/version``,
        ``kit.tar.gz``, ``/kit/{path}`` — is designed to degrade gracefully for
        an anonymous caller, and 503ing all of it because one contract file is
        missing would break exactly the property it exists to have. The failure
        is scoped to the representation that is actually broken.
        """
        build = cls._build_or_cached()
        cls._require_serviceable_contract(build)
        return build.version, build.contract_tarball

    @staticmethod
    def _contract_defect(message: str, *args: Any) -> NoReturn:
        """Log a contract build defect and raise the surface's standard 503."""
        logger.error(message, *args)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local agent kit is not available on this instance",
        )

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
    def _build_or_cached(cls) -> _KitBuild:
        """The current :class:`_KitBuild`, rebuilding if the snapshot changed.

        Both tarballs are packed, and the contract verdict decided, inside the
        one critical section from the one rendered tree — so they can never
        disagree about what the kit at ``kit_version`` contains, and an
        anonymous caller never pays for either.

        This method **never raises on a contract defect**, only on a defect of
        the kit itself (a missing or oversized snapshot). D16 forbids raising
        here because every kit-surface path goes through it: the verdict is
        computed here and *carried*, and only the two contract accessors turn
        it into a 503.
        """
        kit_dir = local_kit_dir()
        cache_key = snapshot_cache_key(kit_dir)

        cached = cls._cache
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        with cls._lock:
            # Re-check inside the lock: another thread may have just built it.
            cached = cls._cache
            if cached is not None and cached[0] == cache_key:
                return cached[1]

            version, rendered = cls._render_tree(kit_dir)
            build = _KitBuild(
                version=version,
                rendered=rendered,
                tarball=cls._build_tarball(rendered),
                contract_tarball=cls._build_tarball(
                    rendered,
                    root=CONTRACT_TARBALL_ROOT,
                    include=cls._is_contract_member,
                ),
                contract_defect=cls._contract_defect_reason(rendered),
            )
            cls._cache = (cache_key, build)
            logger.info(
                "Built local agent kit (%d files, %d tarball bytes, "
                "%d contract tarball bytes, kit_version=%s, cache_key=%s)",
                len(rendered),
                len(build.tarball),
                len(build.contract_tarball),
                version,
                cache_key,
            )
            if build.contract_defect is not None:
                # Logged once per snapshot, here, where the specifics are still
                # in hand. The per-request 503 carries only the surface's
                # generic detail, so this line is the one place a defect is
                # named — at ERROR, so it is not filtered out of a production
                # log by level.
                logger.error(
                    "Local agent kit contract is not serviceable — %s "
                    "(snapshot at %s). The kit itself is unaffected and keeps "
                    "serving; only /contract.tar.gz and /contract/version 503.",
                    build.contract_defect,
                    kit_dir,
                )
            return build

    @staticmethod
    def _is_contract_member(rel_path: str) -> bool:
        """Whether a rendered kit member belongs to the contract subset."""
        return rel_path in CONTRACT_MEMBERS or rel_path.startswith(
            CONTRACT_MEMBER_PREFIXES
        )

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
    def _build_tarball(
        rendered: dict[str, bytes],
        *,
        root: str = TARBALL_ROOT,
        include: Callable[[str], bool] | None = None,
    ) -> bytes:
        """Pack the rendered tree into a gzip tarball rooted at ``root``.

        One packer for both archives. ``include`` selects a subset (the
        contract); ``None`` packs everything (the full kit). Determinism —
        fixed member mtimes, a zeroed gzip header, sorted members — is the
        whole reason this is not duplicated per archive: two packers would be
        two chances to reintroduce a wall-clock timestamp and serve two
        different bodies under one strong ETag.
        """
        buffer = io.BytesIO()
        # Fixed member mtimes and a zeroed gzip header: the same rendered tree
        # must produce the same bytes in every worker and on every rebuild.
        # Wall-clock timestamps would make two workers serve different bodies
        # under one strong ETag, which is precisely what a validator promises
        # cannot happen.
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for rel in sorted(rendered):
                    if include is not None and not include(rel):
                        continue
                    content = rendered[rel]
                    info = tarfile.TarInfo(name=f"{root}/{rel}")
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
