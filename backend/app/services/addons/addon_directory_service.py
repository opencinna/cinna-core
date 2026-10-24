"""``GET /addons/directory``: one index over every addon source core offers.

Three adapters normalise their rows to :class:`DirectoryEntry`:

- **catalog** — published catalog skills the caller may see
  (``SkillCatalogService.list_catalog``), delisted ones only for a maintainer;
- **marketplace** — entries of every marketplace the caller may discover
  (the ``/discover`` rule): a ``skills``-format entry is a ``skill``, a Claude
  or Codex entry a ``plugin``. ``status`` comes from the snapshot
  (``snapshot_status``), never from the cloud ``supported`` verdict;
- **MCP directory** — every entry not banned.

The built index is cached per ``(user, directory version)`` for 60 s; filters,
ranking and paging run per request over it. The ETag hashes the response body,
so a missed version bump costs at most the TTL of staleness, never a wrong 304.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from sqlmodel import Session, select

from app.core.config import settings
from app.models.addons.directory import (
    AddonsDirectoryPublic,
    AddonsPolicy,
    DirectoryArchiveInstall,
    DirectoryEntry,
    DirectoryOrigin,
    DirectoryPublisher,
    DirectoryRef,
    DirectoryRequires,
)
from app.models.plugins.llm_plugin import (
    LLMPluginMarketplace,
    LLMPluginMarketplacePlugin,
)
from app.models.skills.schemas import SkillPackageEntry
from app.models.skills.skill_package_revision import SkillPackageRevision
from app.models.users.user import User
from app.services.addons.directory_version import AddonsDirectoryVersion
from app.services.credentials.desktop_credential_service import (
    LOCALLY_COMPATIBLE_TYPES,
)
from app.services.addons.summary import summary_line
from app.services.mcp.mcp_directory_service import McpDirectoryService
from app.services.plugins.llm_plugin_service import LLMPluginService
from app.services.skills.skill_catalog_service import SkillCatalogService

CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 256
MAX_LIMIT = 5000

_KIND_ORDER = {"skill": 0, "plugin": 1, "mcp": 2}
_LOCAL_CREDENTIAL_TYPES = frozenset(t.value for t in LOCALLY_COMPATIBLE_TYPES)
#: Marketplace rows that cannot run on a computer at all (A3).
_DESKTOP_UNRUNNABLE_REASONS = frozenset({"app_connector_only"})


def _clean(value: str | None) -> str | None:
    """Trimmed text, or None for a missing or blank value."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


@dataclass(frozen=True)
class DirectoryQuery:
    search: str | None = None
    kind: str | None = None
    category: str | None = None
    source: str | None = None
    runs_on: str | None = None
    skip: int = 0
    limit: int | None = None


@dataclass
class _Indexed:
    """An entry plus the facts filters need that the wire shape does not carry."""

    entry: DirectoryEntry
    desktop_runnable: bool


class AddonDirectoryService:
    _cache: OrderedDict[tuple[uuid.UUID, int], tuple[float, list[_Indexed]]] = OrderedDict()
    _cache_lock = threading.Lock()
    #: Catalog ``runs_code`` per revision id; revisions are immutable (A8).
    _revision_runs_code: dict[uuid.UUID, bool] = {}

    # ── Public entry point ─────────────────────────────────────────────

    @staticmethod
    def directory(
        session: Session, user: User, query: DirectoryQuery
    ) -> AddonsDirectoryPublic:
        indexed = AddonDirectoryService.index_for(session, user)
        rows = AddonDirectoryService.apply_filters(indexed, query)
        ranked = AddonDirectoryService.rank([r.entry for r in rows], query.search)
        count = len(ranked)
        end = None if query.limit is None else query.skip + query.limit
        page = ranked[query.skip:end]
        policy = AddonDirectoryService.policy_for(session, user)
        return AddonsDirectoryPublic(
            data=page,
            count=count,
            policy=policy,
            etag=AddonDirectoryService.etag_of(page, count, policy),
        )

    @staticmethod
    def policy_for(session: Session, user: User) -> AddonsPolicy:
        """What this caller's desktop may do. Everything, until admin controls exist."""
        return AddonsPolicy()

    @staticmethod
    def etag_of(data: list[DirectoryEntry], count: int, policy: AddonsPolicy) -> str:
        body = json.dumps(
            {
                "data": [e.model_dump(mode="json") for e in data],
                "count": count,
                "policy": policy.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'

    # ── Cache ──────────────────────────────────────────────────────────

    @classmethod
    def index_for(cls, session: Session, user: User) -> list[_Indexed]:
        key = (user.id, AddonsDirectoryVersion.current(session))
        now = time.monotonic()
        with cls._cache_lock:
            hit = cls._cache.get(key)
            if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
                cls._cache.move_to_end(key)
                return hit[1]
        built = cls.build_index(session, user)
        with cls._cache_lock:
            cls._cache[key] = (now, built)
            cls._cache.move_to_end(key)
            while len(cls._cache) > CACHE_MAX_ENTRIES:
                cls._cache.popitem(last=False)
        return built

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()

    # ── Build ──────────────────────────────────────────────────────────

    @classmethod
    def build_index(cls, session: Session, user: User) -> list[_Indexed]:
        return [
            *cls._catalog_entries(session, user),
            *cls._marketplace_entries(session, user),
            *(
                _Indexed(McpDirectoryService.to_directory_entry(row), True)
                for row in McpDirectoryService.visible_entries(session)
            ),
        ]

    @classmethod
    def _catalog_entries(cls, session: Session, user: User) -> list[_Indexed]:
        packages = [
            p
            for p in SkillCatalogService.list_catalog(session, user)
            if (p.is_listed or p.can_manage) and p.latest_revision is not None
        ]
        runs_code = cls._catalog_runs_code(
            session, [p.latest_revision.id for p in packages]
        )
        result: list[_Indexed] = []
        for package in packages:
            revision = package.latest_revision
            credentials = list(revision.required_credentials or [])
            result.append(
                _Indexed(
                    entry=cls._catalog_entry(
                        package, runs_code.get(revision.id, False)
                    ),
                    desktop_runnable=all(
                        c.type in _LOCAL_CREDENTIAL_TYPES for c in credentials
                    ),
                )
            )
        return result

    @staticmethod
    def _catalog_entry(package: SkillPackageEntry, runs_code: bool) -> DirectoryEntry:
        revision = package.latest_revision
        assert revision is not None
        description = package.description or (revision.frontmatter or {}).get(
            "description"
        )
        if not isinstance(description, str):
            description = None
        return DirectoryEntry(
            ref=DirectoryRef(source="core-catalog", id=str(package.id)),
            kind="skill",
            name=package.name,
            title=package.display_name or package.name,
            summary=summary_line(description),
            description=description,
            category=None,
            tags=[],
            icon_url=None,
            publisher=DirectoryPublisher(
                name=_clean(package.publisher_name) or _clean(package.publisher_email),
                verified=bool(package.publisher_email_confirmed),
            ),
            origin=DirectoryOrigin(label="Catalog", kind="catalog"),
            version=_clean(package.latest_version) or _clean(revision.version),
            digest=f"sha256:{revision.content_hash}" if revision.content_hash else None,
            components=None,
            requires=DirectoryRequires(credentials=list(revision.required_credentials or [])),
            runs_code=runs_code,
            status="ready",
            unsupported_reason=None,
            install=DirectoryArchiveInstall(
                archive_path=(
                    f"{settings.API_V1_STR}/skills/packages/{package.id}"
                    f"/revisions/{revision.revision_number}/download"
                )
            ),
        )

    @classmethod
    def _catalog_runs_code(
        cls, session: Session, revision_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, bool]:
        """Whether each revision ships ``scripts/`` (A8), cached per revision."""
        missing = [rid for rid in revision_ids if rid not in cls._revision_runs_code]
        if missing:
            revisions = session.exec(
                select(SkillPackageRevision).where(SkillPackageRevision.id.in_(missing))
            ).all()
            for revision in revisions:
                found = cls._ships_scripts(revision)
                if found is not None:
                    cls._revision_runs_code[revision.id] = found
        return {rid: cls._revision_runs_code.get(rid, False) for rid in revision_ids}

    @staticmethod
    def _ships_scripts(revision: SkillPackageRevision) -> bool | None:
        """None when the snapshot is not on disk: not cached, so a restored
        storage root is seen on the next build."""
        skill_dir = SkillCatalogService.skill_dir_of(revision)
        if not skill_dir.is_dir():
            return None
        return any(
            path.relative_to(skill_dir).parts[0] == "scripts"
            for path in SkillCatalogService.snapshot_files(skill_dir)
        )

    @classmethod
    def _marketplace_entries(cls, session: Session, user: User) -> list[_Indexed]:
        statement = (
            select(LLMPluginMarketplacePlugin, LLMPluginMarketplace)
            .join(
                LLMPluginMarketplace,
                LLMPluginMarketplace.id == LLMPluginMarketplacePlugin.marketplace_id,
            )
            .where(LLMPluginService._discoverable_clause(user.id))
        )
        return [
            _Indexed(
                entry=cls._marketplace_entry(plugin, marketplace),
                desktop_runnable=plugin.unsupported_reason
                not in _DESKTOP_UNRUNNABLE_REASONS,
            )
            for plugin, marketplace in session.exec(statement).all()
        ]

    @staticmethod
    def _marketplace_entry(
        plugin: LLMPluginMarketplacePlugin, marketplace: LLMPluginMarketplace
    ) -> DirectoryEntry:
        is_skill = marketplace.type == "skills"
        snapshot = plugin.snapshot_status
        if snapshot == "ready":
            status = "ready"
        elif snapshot == "failed":
            status = "unsupported"
        else:
            # ``pending``, or a row not re-synced since snapshots existed: the
            # next sync or worker tick gives it a verdict.
            status = "pending"
        reason = (
            (plugin.snapshot_reason or plugin.unsupported_reason)
            if status == "unsupported"
            else None
        )
        sha = plugin.archive_sha256 if status == "ready" else None

        config = plugin.config if isinstance(plugin.config, dict) else {}
        keywords = config.get("keywords") or config.get("tags") or []
        tags = [k for k in keywords if isinstance(k, str)] if isinstance(keywords, list) else []

        owner = LLMPluginService.marketplace_owner_label(marketplace)
        author = _clean(plugin.author_name) or _clean(plugin.author_email)
        owner_name = _clean(marketplace.owner_name)
        owner_email = _clean(marketplace.owner_email)
        author_name = _clean(plugin.author_name)
        author_email = _clean(plugin.author_email)
        # A4: first-party only when the entry names an author and that author
        # is the marketplace owner. No author is no claim, not a verification.
        first_party = author is not None and (
            (author_name is not None and author_name == owner_name)
            or (
                author_email is not None
                and owner_email is not None
                and author_email.casefold() == owner_email.casefold()
            )
        )

        components = LLMPluginService._components_public(plugin)
        runs_code = plugin.runs_code
        if runs_code is None:
            summary = LLMPluginService._skill_summary(plugin)
            runs_code = bool(summary and summary.has_scripts)

        return DirectoryEntry(
            ref=DirectoryRef(source="core-marketplace", id=str(plugin.id)),
            kind="skill" if is_skill else "plugin",
            name=plugin.name,
            title=plugin.name,
            summary=summary_line(plugin.description),
            description=plugin.description,
            category=plugin.category,
            tags=tags,
            icon_url=None,
            publisher=DirectoryPublisher(name=author or owner, verified=first_party),
            origin=DirectoryOrigin(
                label=marketplace.name,
                kind="marketplace",
                repository_url=LLMPluginService.plugin_repository_url(plugin, marketplace.url),
                homepage=plugin.homepage,
            ),
            version=_clean(plugin.version),
            digest=f"sha256:{plugin.archive_sha256}" if plugin.archive_sha256 else None,
            components=components,
            requires=DirectoryRequires(),
            runs_code=bool(runs_code),
            status=status,
            unsupported_reason=reason,
            install=(
                DirectoryArchiveInstall(
                    archive_path=(
                        f"{settings.API_V1_STR}/llm-plugins/plugins/{plugin.id}"
                        f"/archive?sha256={sha}"
                    )
                )
                if sha
                else None
            ),
        )

    # ── Filter / rank ──────────────────────────────────────────────────

    @staticmethod
    def apply_filters(rows: list[_Indexed], query: DirectoryQuery) -> list[_Indexed]:
        def keep(row: _Indexed) -> bool:
            entry = row.entry
            if query.kind and entry.kind != query.kind:
                return False
            if query.source and entry.ref.source != query.source:
                return False
            if query.category:
                wanted = query.category.lower()
                if (entry.category or "").lower() != wanted and wanted not in (
                    t.lower() for t in entry.tags
                ):
                    return False
            if query.runs_on == "desktop" and not row.desktop_runnable:
                return False
            return True

        return [row for row in rows if keep(row)]

    @staticmethod
    def match_tier(entry: DirectoryEntry, needle: str) -> int | None:
        """exact name 0 > name prefix 1 > title 2 > tags/category 3 > description 4."""
        name = entry.name.lower()
        if name == needle:
            return 0
        if name.startswith(needle):
            return 1
        if needle in entry.title.lower():
            return 2
        if needle in (entry.category or "").lower() or any(
            needle in tag.lower() for tag in entry.tags
        ):
            return 3
        if needle in (entry.description or "").lower() or needle in (
            entry.summary or ""
        ).lower():
            return 4
        return None

    @staticmethod
    def rank(entries: list[DirectoryEntry], search: str | None) -> list[DirectoryEntry]:
        needle = (search or "").strip().lower()
        if not needle:
            return sorted(
                entries,
                key=lambda e: (_KIND_ORDER[e.kind], e.title.lower(), e.ref.id),
            )
        tiered = [
            (tier, entry)
            for entry in entries
            if (tier := AddonDirectoryService.match_tier(entry, needle)) is not None
        ]
        tiered.sort(
            key=lambda pair: (
                pair[0],
                pair[1].status == "unsupported",
                pair[1].title.lower(),
                pair[1].ref.id,
            )
        )
        return [entry for _tier, entry in tiered]
