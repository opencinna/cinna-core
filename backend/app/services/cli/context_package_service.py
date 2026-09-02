"""
Account-CLI context package.

Assembles the *orchestrator context package* downloaded by ``cinna account
setup`` into the account workspace's ``context/`` tree. The package is the
platform's self-knowledge, delivered to a local coding agent that drives a
multi-agent network from the account root.

Contents (static platform knowledge only — never any user-specific secret):

  context/
    README.md                 # package index the orchestrator CLAUDE.md points at
    VERSION                   # content hash of this package (staleness check)
    platform/                 # curated business-logic docs (glossary, feature map)
      README.md               #   = docs/README.md (the feature map entrypoint)
      application/ agents/    #   business-logic feature docs (no *_tech files)
    api_reference/            # generated REST API reference, one file per domain
    examples/                 # working API-script patterns (platform_helper + samples)
    guides/                   # hand-authored worked playbooks (e.g. build a network)
    local-kit/                # the Local Agent Kit, rendered — conventions for
                              #   agents built locally with a coding assistant

Source of truth
---------------
The package is assembled from the committed ``platform-knowledge-env`` template
snapshot (``…/knowledge/platform/`` + ``…/scripts/examples/`` +
``…/knowledge/guides/`` + ``…/knowledge/local-kit/``). That snapshot is
the only copy of this knowledge present inside the backend container at runtime
— the repo-root ``docs/`` tree and ``frontend/openapi.json`` are not shipped in
the image. The snapshot is refreshed by
``.cinna-core-kit/scripts/sync_platform_knowledge.py`` (which shares the
API-reference generation logic with this service via
``platform_knowledge_assets``).

Transport: a gzip tarball, mirroring the per-agent workspace clone
(``CLIService.get_workspace_tarball``), so the CLI reuses one extract path.

Freshness: the snapshot only changes when the sync script is re-run (a deploy
artifact, not per-request work). The built tarball is therefore cached in-process
and keyed by the snapshot directories' newest mtime, so a redeploy that ships a
fresh snapshot invalidates the cache automatically without per-request tar work.

Staleness, on the other side of the wire, is the CLI's problem: a workspace set
up before a guide existed has no way to notice. So the package carries a
**content** version — ``context/VERSION``, also served as the
``X-Context-Package-Version`` response header and by
``GET /cli/account/context-package/version`` — that the CLI can compare against
what is on disk. It is a hash of the packaged content, deliberately not the
mtime-based cache key: a redeploy that ships byte-identical knowledge must not
tell every workspace it is behind.
"""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
import threading
from pathlib import Path

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from app.services.cli.local_agent_kit_service import LocalAgentKitService
from app.services.cli.platform_knowledge_assets import (
    example_scripts_dir,
    guides_dir as guides_snapshot_dir,
    local_kit_dir,
    platform_knowledge_dir,
    snapshot_cache_key,
)

logger = logging.getLogger(__name__)

# Inside the package, the curated platform docs (everything under the snapshot's
# knowledge/platform/ EXCEPT the generated api_reference/) land under platform/,
# and the API reference is promoted to a top-level api_reference/ tree.
_API_REFERENCE_SUBDIR = "api_reference"

# Where the package stamps its own content version, and the header that carries
# it on the download. The CLI compares the two to decide whether an existing
# workspace's ``context/`` tree is behind.
CONTEXT_PACKAGE_VERSION_MEMBER = "context/VERSION"
CONTEXT_PACKAGE_VERSION_HEADER = "X-Context-Package-Version"

# Where the Local Agent Kit lands inside the package, and the label its files
# carry in the content hash.
_LOCAL_KIT_SUBDIR = "local-kit"


class ContextPackageService:
    """Builds (and caches) the account-CLI orchestrator context package."""

    # (cache_key, content_version, tarball_bytes) — process-local memoization of
    # the built tarball. ``cache_key`` is the cheap mtime probe; ``content_version``
    # is the stable hash handed to callers.
    _cache: tuple[str, str, bytes] | None = None
    _lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────────

    @classmethod
    def get_context_package(cls) -> StreamingResponse:
        """
        Return the context package as a streamed gzip tarball.

        Mirrors ``CLIService.get_workspace_tarball`` (same media type and
        attachment disposition) so the CLI's existing tarball-extract path is
        reused verbatim.
        """
        version, content = cls._build_or_cached()

        async def content_iter():
            yield content

        return StreamingResponse(
            content_iter(),
            media_type="application/tar+gzip",
            headers={
                "Content-Disposition": 'attachment; filename="context-package.tar.gz"',
                # Lets a caller that already extracted this package tell whether
                # it is current without re-downloading it.
                CONTEXT_PACKAGE_VERSION_HEADER: version,
                "Access-Control-Expose-Headers": CONTEXT_PACKAGE_VERSION_HEADER,
            },
        )

    @classmethod
    def get_content_version(cls) -> str:
        """The current package's content version (the ``context/VERSION`` value).

        Building to answer this is the same work the download would do, and the
        result is cached, so the version endpoint stays cheap after the first
        call in a process.
        """
        return cls._build_or_cached()[0]

    # ── Build / cache ────────────────────────────────────────────────────

    @classmethod
    def _build_or_cached(cls) -> tuple[str, bytes]:
        platform_dir = platform_knowledge_dir()
        examples_dir = example_scripts_dir()
        guides_dir = guides_snapshot_dir()
        # Shared with LocalAgentKitService: one mtime+count probe shape for
        # every consumer that memoizes work derived from the snapshot. The kit
        # directory is part of the key so an edited kit rebuilds this package
        # too — the kit ships *inside* it.
        cache_key = snapshot_cache_key(
            platform_dir, examples_dir, guides_dir, local_kit_dir()
        )

        cached = cls._cache
        if cached is not None and cached[0] == cache_key:
            return cached[1], cached[2]

        with cls._lock:
            # Re-check inside the lock: another thread may have just built it.
            cached = cls._cache
            if cached is not None and cached[0] == cache_key:
                return cached[1], cached[2]

            index = cls._render_index()
            local_kit = cls._local_kit_tree()
            content_version = cls._content_version(
                platform_dir, examples_dir, guides_dir, local_kit, index
            )
            content = cls._build_tarball(
                platform_dir, examples_dir, guides_dir, local_kit, index, content_version
            )
            cls._cache = (cache_key, content_version, content)
            logger.info(
                "Built account context package (%d bytes, content_version=%s, "
                "cache_key=%s)",
                len(content),
                content_version,
                cache_key,
            )
            return content_version, content

    @staticmethod
    def _local_kit_tree() -> dict[str, bytes]:
        """The rendered Local Agent Kit, or ``{}`` when this image has none.

        Rendered, never the raw snapshot: the packaged copy has to carry this
        instance's URLs, exactly like the one an assistant downloads from
        ``/agent-start``. Reusing ``LocalAgentKitService``'s own memoized build also
        keeps the two byte-identical, so a cloud orchestrator reading
        ``context/local-kit/`` and a local assistant reading the tarball never
        disagree about the conventions.

        Absence is tolerated like ``examples/`` and ``guides/``: the kit tells a
        cloud orchestrator how a locally built agent is laid out, which is
        helpful but not the core knowledge. ``LocalAgentKitService`` raises 503
        on a missing snapshot because *its own* endpoint has nothing else to
        serve; here that must not take the whole package down.

        The instance's ``local_agent_kit_enabled`` switch is deliberately not
        consulted: it governs the *public, anonymous* surface. This package is
        served to an authenticated account workspace, and an operator who
        stopped publishing to strangers has not thereby withdrawn the
        conventions from their own users.
        """
        try:
            _, rendered = LocalAgentKitService.get_rendered_tree()
        except Exception:
            # Deliberately every exception, not just the 503 the service raises
            # for a missing snapshot: it also stats and reads every file in the
            # tree, so a bad mount or a half-extracted image layer surfaces as
            # an OSError. Letting that through would turn a fault in the
            # *optional* source into a 500 on the whole package — exactly the
            # failure this degradation exists to prevent.
            logger.warning(
                "Context package: local agent kit unavailable at %s — serving "
                "package without local-kit/",
                local_kit_dir(),
                exc_info=True,
            )
            return {}
        return rendered

    @staticmethod
    def _content_version(
        platform_dir: Path,
        examples_dir: Path,
        guides_dir: Path,
        local_kit: dict[str, bytes],
        index: str,
    ) -> str:
        """Stable hash of everything the package ships.

        Path + content of every packaged file, plus the rendered index (which is
        code, not snapshot, and changes independently of it). Paths are taken
        relative to their source root so the digest does not move between
        machines, and mtimes are deliberately not folded in: a rebuild that
        ships identical knowledge must produce an identical version, or every
        workspace is told it is stale on every deploy and the signal stops
        meaning anything.
        """
        digest = hashlib.sha256()
        for label, root in (
            ("platform", platform_dir),
            ("examples", examples_dir),
            ("guides", guides_dir),
        ):
            if not root.is_dir():
                continue
            for file in sorted(p for p in root.rglob("*") if p.is_file()):
                rel = file.relative_to(root).as_posix()
                digest.update(f"{label}/{rel}\0".encode("utf-8"))
                digest.update(hashlib.sha256(file.read_bytes()).digest())
        # The kit is folded in from its *rendered* bytes, not from disk: the
        # rendering is what ships, and it moves when this instance's settings do.
        for rel in sorted(local_kit):
            digest.update(f"{_LOCAL_KIT_SUBDIR}/{rel}\0".encode("utf-8"))
            digest.update(hashlib.sha256(local_kit[rel]).digest())
        digest.update(b"index\0")
        digest.update(index.encode("utf-8"))
        return digest.hexdigest()[:16]

    @classmethod
    def _build_tarball(
        cls,
        platform_dir: Path,
        examples_dir: Path,
        guides_dir: Path,
        local_kit: dict[str, bytes],
        index: str,
        content_version: str,
    ) -> bytes:
        """
        Assemble the package tarball in memory.

        Fail-loud discipline (cf. bundle publish on a missing workspace): the
        platform docs + generated API reference are the essential payload, and
        their absence means the snapshot was never baked into this Docker image
        — a broken build, not a degraded-but-usable state. Rather than serve a
        near-empty 200 that silently masks that, we raise 503 so the caller sees
        the capability is unavailable in this deployment. The exception
        propagates out of ``_build_or_cached`` before the result is cached, so a
        failure is never memoized.

        A missing ``examples/``, ``guides/`` or ``local-kit/`` tree alone is
        tolerable (warn + omit): the example scripts, worked playbooks and local
        agent kit are helpful but not the core knowledge, so we degrade
        gracefully on those rather than fail the whole download.
        """
        platform_files = (
            sorted(p for p in platform_dir.rglob("*") if p.is_file())
            if platform_dir.is_dir()
            else []
        )
        if not platform_files:
            logger.error(
                "Context package: platform knowledge snapshot missing or empty "
                "at %s — this deployment's image was built without the "
                "platform-knowledge-env snapshot",
                platform_dir,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Platform context snapshot unavailable in this deployment",
            )

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # 1. Curated platform docs + API reference from the GA snapshot.
            api_ref_dir = platform_dir / _API_REFERENCE_SUBDIR
            for file in platform_files:
                rel = file.relative_to(platform_dir)
                # Promote api_reference/ to a top-level tree; everything else
                # nests under platform/.
                try:
                    api_rel = file.relative_to(api_ref_dir)
                    arcname = f"context/{_API_REFERENCE_SUBDIR}/{api_rel}"
                except ValueError:
                    arcname = f"context/platform/{rel}"
                tar.add(file, arcname=arcname)

            # 2. Example API-script patterns the orchestrator prompt references.
            #    Non-essential: omit (with a warning) if the snapshot lacks them.
            if examples_dir.is_dir():
                for file in sorted(examples_dir.rglob("*")):
                    if not file.is_file():
                        continue
                    rel = file.relative_to(examples_dir)
                    tar.add(file, arcname=f"context/examples/{rel}")
            else:
                logger.warning(
                    "Context package: example scripts snapshot missing at %s — "
                    "serving package without examples/",
                    examples_dir,
                )

            # 3. Hand-authored worked playbooks (e.g. build-an-agentic-network).
            #    Non-essential like examples/: omit (with a warning) if absent.
            if guides_dir.is_dir():
                for file in sorted(guides_dir.rglob("*")):
                    if not file.is_file():
                        continue
                    rel = file.relative_to(guides_dir)
                    tar.add(file, arcname=f"context/guides/{rel}")
            else:
                logger.warning(
                    "Context package: guides snapshot missing at %s — "
                    "serving package without guides/",
                    guides_dir,
                )

            # 4. The Local Agent Kit, rendered for this instance, so a cloud
            #    orchestrator importing a locally built agent reads the same
            #    conventions the local assistant followed. Already warned about
            #    upstream when absent (see `_local_kit_tree`).
            for rel in sorted(local_kit):
                member = local_kit[rel]
                info = tarfile.TarInfo(name=f"context/{_LOCAL_KIT_SUBDIR}/{rel}")
                info.size = len(member)
                # Same mode rule as the kit's own tarball
                # (`LocalAgentKitService._build_tarball`): `tools/kit.py` is a
                # program in both copies, and the point of packaging the
                # rendered tree is that the two copies are the same thing.
                info.mode = 0o755 if rel.endswith(".py") else 0o644
                tar.addfile(info, io.BytesIO(member))

            # 5. Package index the orchestrator CLAUDE.md points at.
            index_bytes = index.encode("utf-8")
            info = tarfile.TarInfo(name="context/README.md")
            info.size = len(index_bytes)
            tar.addfile(info, io.BytesIO(index_bytes))

            # 6. The version stamp, so an extracted workspace knows which
            #    package it holds without keeping state anywhere else.
            version_bytes = f"{content_version}\n".encode("utf-8")
            info = tarfile.TarInfo(name=CONTEXT_PACKAGE_VERSION_MEMBER)
            info.size = len(version_bytes)
            tar.addfile(info, io.BytesIO(version_bytes))

        return buf.getvalue()

    # ── Index ────────────────────────────────────────────────────────────

    @staticmethod
    def _render_index() -> str:
        """The ``context/README.md`` index for the orchestrator agent."""
        return (
            "# Cinna Platform Context Package\n"
            "\n"
            "Static platform knowledge for orchestrating your agent network from\n"
            "this account workspace. Downloaded by `cinna account setup`; refreshed\n"
            "by re-running setup. Contains no secrets — only platform documentation\n"
            "and the generated REST API reference.\n"
            "\n"
            "## Layout\n"
            "\n"
            "| Path | What it is |\n"
            "|------|------------|\n"
            "| `platform/README.md` | Platform feature map — the entrypoint. Start here. |\n"
            "| `platform/application/` | Business-logic docs for user-facing features. |\n"
            "| `platform/agents/` | Business-logic docs for agent-side features. |\n"
            "| `api_reference/README.md` | Index of the REST API reference by domain. |\n"
            "| `api_reference/*.md` | Generated endpoint reference, one file per domain. |\n"
            "| `examples/` | Working API-script patterns (`platform_helper.py` + samples). |\n"
            "| `local-kit/` | Conventions for agents built locally with a coding assistant; read `local-kit/guides/11-go-cloud.md` when importing one. |\n"
            "| `guides/` | Worked walkthroughs — stand up a delegating multi-agent network, expose an agent as a REST API, author an agent's prompts & description, and turn a user's improvement request into a fix. |\n"
            "| `VERSION` | Content version of this package. Compare it against `GET /api/v1/cli/account/context-package/version` to find out whether this workspace is behind; `cinna account refresh-context` brings it up to date. |\n"
            "\n"
            "## How to use this\n"
            "\n"
            "1. Read `platform/README.md` to find the feature(s) relevant to the\n"
            "   user's request, then open the matching `platform/application/` or\n"
            "   `platform/agents/` doc for the business rules.\n"
            "2. Consult `api_reference/` for the exact endpoints, request bodies,\n"
            "   and response shapes.\n"
            "3. Adapt the patterns in `examples/` for authenticated API calls.\n"
            "\n"
            "To act on a specific agent, use `cinna agent sync <agent>` to attach a\n"
            "standard per-agent dev workspace under `agents/<agent>/`, then drive it\n"
            "with the normal `cinna dev` / `cinna exec` loop.\n"
            "\n"
            "When you finish building an agent, read\n"
            "`guides/authoring-agent-prompts.md` and complete the finalize step:\n"
            "author the agent's prompts from what you actually built and rewrite its\n"
            "**description** to match the finished agent, then assign them in one\n"
            "bulk write (`cinna api PUT agents/<id> --data @agents/<name>/prompts.json`).\n"
            "\n"
            "When writing `example_prompts`, remember they are shown to a different\n"
            "person, later, with different data than your build. Never freeze in\n"
            "values from your own build session (the URL you tested, a fixture id, a\n"
            "sample file, today's date). Each example is either universal and\n"
            "sendable as-is (`\"What is my status today?\"`) or an obviously\n"
            "unfinished template the user completes (`\"Investigate this URL — <paste\n"
            "the URL here>\"`). See the guide for the full rule.\n"
            "\n"
            "When the user asks you to check or act on **improvement requests** —\n"
            "sessions their users shared back because an agent handled something\n"
            "badly — read `guides/handling-improvement-requests.md` first. It covers\n"
            "the `cinna improve list|show|download|status` loop, how to tell which\n"
            "install you may actually fix (a bundle fix belongs in the publisher\n"
            "install and ships as a new version), when to implement versus stop and\n"
            "ask, and the rules for handling another person's conversation data.\n"
        )
