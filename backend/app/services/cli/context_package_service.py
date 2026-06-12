"""
Account-CLI context package.

Assembles the *orchestrator context package* downloaded by ``cinna account
setup`` into the account workspace's ``context/`` tree. The package is the
General Assistant's platform self-knowledge, re-delivered to a local coding
agent that drives a multi-agent network from the account root.

Contents (static platform knowledge only — never any user-specific secret):

  context/
    README.md                 # package index the orchestrator CLAUDE.md points at
    platform/                 # curated business-logic docs (glossary, feature map)
      README.md               #   = docs/README.md (the feature map entrypoint)
      application/ agents/    #   business-logic feature docs (no *_tech files)
    api_reference/            # generated REST API reference, one file per domain
    examples/                 # working API-script patterns (platform_helper + samples)
    guides/                   # hand-authored worked playbooks (e.g. build a network)

Source of truth
---------------
The package is assembled from the committed ``general-assistant-env`` template
snapshot (``…/knowledge/platform/`` + ``…/scripts/examples/`` +
``…/knowledge/guides/``). That snapshot is
the only copy of this knowledge present inside the backend container at runtime
— the repo-root ``docs/`` tree and ``frontend/openapi.json`` are not shipped in
the image. The snapshot is refreshed by
``.cinna-core-kit/scripts/sync_ga_knowledge.py`` (which shares the API-reference
generation logic with this service via ``ga_knowledge_assets``).

Transport: a gzip tarball, mirroring the per-agent workspace clone
(``CLIService.get_workspace_tarball``), so the CLI reuses one extract path.

Freshness: the snapshot only changes when the sync script is re-run (a deploy
artifact, not per-request work). The built tarball is therefore cached in-process
and keyed by the snapshot directories' newest mtime, so a redeploy that ships a
fresh snapshot invalidates the cache automatically without per-request tar work.
"""

from __future__ import annotations

import io
import logging
import tarfile
import threading
from pathlib import Path

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from app.services.cli.ga_knowledge_assets import (
    ga_example_scripts_dir,
    ga_guides_dir,
    ga_platform_knowledge_dir,
)

logger = logging.getLogger(__name__)

# Inside the package, the curated platform docs (everything under the snapshot's
# knowledge/platform/ EXCEPT the generated api_reference/) land under platform/,
# and the API reference is promoted to a top-level api_reference/ tree.
_API_REFERENCE_SUBDIR = "api_reference"


class ContextPackageService:
    """Builds (and caches) the account-CLI orchestrator context package."""

    # (cache_key, tarball_bytes) — process-local memoization of the built tarball.
    _cache: tuple[str, bytes] | None = None
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
        content = cls._build_or_cached()

        async def content_iter():
            yield content

        return StreamingResponse(
            content_iter(),
            media_type="application/tar+gzip",
            headers={
                "Content-Disposition": 'attachment; filename="context-package.tar.gz"',
            },
        )

    # ── Build / cache ────────────────────────────────────────────────────

    @classmethod
    def _build_or_cached(cls) -> bytes:
        platform_dir = ga_platform_knowledge_dir()
        examples_dir = ga_example_scripts_dir()
        guides_dir = ga_guides_dir()
        cache_key = cls._snapshot_version(platform_dir, examples_dir, guides_dir)

        cached = cls._cache
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        with cls._lock:
            # Re-check inside the lock: another thread may have just built it.
            cached = cls._cache
            if cached is not None and cached[0] == cache_key:
                return cached[1]

            content = cls._build_tarball(platform_dir, examples_dir, guides_dir)
            cls._cache = (cache_key, content)
            logger.info(
                "Built account context package (%d bytes, version=%s)",
                len(content),
                cache_key,
            )
            return content

    @staticmethod
    def _snapshot_version(
        platform_dir: Path, examples_dir: Path, guides_dir: Path
    ) -> str:
        """
        Cache key derived from the newest mtime AND file count across the
        snapshot sources.

        A redeploy that ships a freshly-synced snapshot bumps file mtimes, which
        changes the key and invalidates the cached tarball automatically. The
        file count is folded in so a pure deletion (which leaves the max mtime
        unchanged) still invalidates the cache — belt-and-suspenders, since the
        sync script rewrites the whole tree anyway. The guides dir is included so
        adding or editing a hand-authored playbook also invalidates the cache.
        """
        newest = 0.0
        count = 0
        for root in (platform_dir, examples_dir, guides_dir):
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.is_file():
                    count += 1
                    mtime = p.stat().st_mtime
                    if mtime > newest:
                        newest = mtime
        return f"{newest:.6f}:{count}"

    @classmethod
    def _build_tarball(
        cls, platform_dir: Path, examples_dir: Path, guides_dir: Path
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

        A missing ``examples/`` or ``guides/`` directory alone is tolerable
        (warn + omit): the example scripts and worked playbooks are helpful but
        not the core knowledge, so we degrade gracefully on those rather than
        fail the whole download.
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
                "general-assistant-env snapshot",
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

            # 4. Package index the orchestrator CLAUDE.md points at.
            index = cls._render_index().encode("utf-8")
            info = tarfile.TarInfo(name="context/README.md")
            info.size = len(index)
            tar.addfile(info, io.BytesIO(index))

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
            "| `guides/` | Worked walkthroughs — stand up a delegating multi-agent network end-to-end. |\n"
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
        )
