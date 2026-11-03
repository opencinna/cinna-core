"""AgentSkillsService — reads and caches the agent's skill index.

An agent's skills live in its workspace at ``skills/<name>/SKILL.md`` (plus the
skills that arrive with installed plugins). env-core parses them and serves the
index at ``GET /config/skills``; this service pulls that index and caches it on
the :class:`~app.models.environments.environment.AgentEnvironment` row so the
agent page, the ``/skills`` command and the slash-command popup can read it
without waking a suspended container.

Same shape as :class:`~app.services.agents.cli_commands_service.CLICommandsService`
— that is deliberate, it is the established pull-only cache pattern — with two
differences that matter:

* **Write short-circuit.** When the reported index equals the cached one,
  nothing is written and no event is emitted: the refresh is one HTTP round
  trip and the DB is untouched. This is the common case, because it fires after
  every stream and every cron run. The comparison is on the index CONTENT, not
  on the reported tree hash — the hash covers the workspace ``skills/`` folder
  only, while the index also carries plugin skills.
* **Its own rate-limit bucket.** The 30 s per-environment window is independent
  of the CLI-commands one; two caches sharing a bucket would let a busy agent's
  status pull starve its skills pull.

The cache is env-authoritative and never written back: nothing here can change
what the engine sees. A stale cache costs a wrong card, never a wrong agent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from app.services.agents.skill_manifest import (
    MAX_DESCRIPTION_LENGTH,
    MAX_SKILL_CREDENTIALS,
    MAX_SLOT_LENGTH,
    SKILL_CREDENTIAL_TYPES,
    SKILL_NAME_RE,
    SKIP_DIR_NAMES,
    SkillEntry,
    coerce_version,
    issue_from_dict,
    parse_frontmatter,
)
from app.services.environments.adapters.base import EndpointUnsupportedError
from app.services.environments.synced_files import SYNCED_FILES
from app.services.environments.workspace_classification import WORKSPACE_ROOT_REL

if TYPE_CHECKING:  # pragma: no cover — import cycle guard, typing only
    from app.models.agents.agent import Agent
    from app.models.agents.agent_skills import SkillEntryPublic, SkillIssuePublic
    from app.models.skills.schemas import SkillRevisionFilePublic

logger = logging.getLogger(__name__)


def _is_skill_folder_path(path: str | None) -> bool:
    """True when ``path`` has the shape of a skill folder, workspace-relative.

    The two ``rel_root`` shapes ``scan_skills_root`` builds entry paths from:
    the agent's own ``skills/<name>`` and a plugin's
    ``plugins/<mkt>/<plugin>/skills/<name>``. Anything else — ``"."``, a
    top-level folder, a ``..`` climb — is not a skill folder, whatever the
    index row claims.
    """
    if not path:
        return False
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if len(parts) == 2:
        return parts[0] == "skills"
    return parts[0] == "plugins" and len(parts) >= 4 and parts[-2] == "skills"


def _walk_skill_folder(
    workspace: Path, rel_path: str
) -> Iterator[tuple[str, os.stat_result]]:
    """Yield ``(relative_posix_path, stat_result)`` for every regular file.

    Descends from ``workspace`` one component at a time with ``O_NOFOLLOW`` and
    walks by file descriptor with ``os.fwalk(follow_symlinks=False)``, so a
    symlink the container swaps in after the path was checked — at the folder,
    above it, or anywhere below it — is refused rather than followed off the
    mount. Skips ``SKIP_DIR_NAMES`` like the index's own walk, in the same
    sorted order.

    Raises:
        OSError: a component of ``rel_path`` is missing or is not a real
            directory.
    """
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel_path.split("/"):
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        for dir_path, dir_names, file_names, dir_fd in os.fwalk(
            ".", dir_fd=fd, follow_symlinks=False
        ):
            dir_names[:] = sorted(d for d in dir_names if d not in SKIP_DIR_NAMES)
            for file_name in sorted(file_names):
                try:
                    stat_result = os.stat(
                        file_name, dir_fd=dir_fd, follow_symlinks=False
                    )
                except OSError:
                    continue
                if not S_ISREG(stat_result.st_mode):
                    continue
                yield posixpath.normpath(
                    posixpath.join(dir_path, file_name)
                ), stat_result
    finally:
        os.close(fd)

# Module-level rate-limit bucket: env_id -> last_fetch_at (UTC). Independent of
# the CLI-commands and status buckets by design (see the module docstring).
_rate_limit_lock: dict[UUID, datetime] = {}

#: Registry key of this cache, and the watched path env-core reports when the
#: folder changes. Read from the registry rather than restated, so the two can
#: never disagree.
SKILLS_REGISTRY_KEY = "skills"
SKILLS_DIR_PATH = next(
    entry.rel_path
    for entry in SYNCED_FILES
    if entry.key == SKILLS_REGISTRY_KEY
)

#: Per-environment refresh window. Matches the CLI-commands cache.
FORCE_REFRESH_TTL_SECONDS = 30

#: Hard cap on cached entries. env-core already applies the 50-skill budget;
#: this is the defence against a compromised or wildly out-of-date container
#: filling a JSON column.
MAX_CACHED_SKILLS = 200

#: Hard cap on a served ``SKILL.md``. Comfortably above the 64 KB body the
#: parser flags as ``oversized``, so an over-long skill is still readable in the
#: viewer instead of vanishing from it.
MAX_CONTENT_BYTES = 256 * 1024

#: Statuses that genuinely mean "asleep". A transitional status
#: (``activating`` / ``starting``) is deliberately NOT here: the env-start
#: sweep runs while the row still reads ``activating`` on a container that is
#: already up, so a failure then is about the container, not about it sleeping
#: — and telling the user to "refresh to wake it" instead of "rebuild it" would
#: send them to the wrong fix.
SLEEPING_STATUSES: frozenset[str] = frozenset({"suspended", "stopped", "error"})

#: Values of ``AgentEnvironment.skills_error``.
#:
#: The split between ``adapter_error`` and ``adapter_unsupported`` is the whole
#: point of this vocabulary: they are both "the call failed on a container that
#: is not asleep", but one is fixed by a restart and the other only by a
#: rebuild. While they shared a code, every surface had to pick one remedy and
#: was therefore wrong half the time — the card told an unreachable env to
#: rebuild, the CLI told a pre-feature container to restart.
ERROR_ENV_NOT_RUNNING = "env_not_running"
ERROR_ADAPTER_ERROR = "adapter_error"
ERROR_ADAPTER_UNSUPPORTED = "adapter_unsupported"
ERROR_PARSE_ERROR = "parse_error"


class SkillsIndexUnavailableError(Exception):
    """Raised when the skill index cannot be fetched from the environment."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AgentSkillsService:
    """Fetch, cache and serve the agent's skill index."""

    # ── Rate-limit helpers ─────────────────────────────────────────────

    @classmethod
    def is_rate_limited(cls, environment_id: UUID) -> bool:
        """True when this environment was fetched within the last 30 seconds."""
        last = _rate_limit_lock.get(environment_id)
        if last is None:
            return False
        return (datetime.now(UTC) - last).total_seconds() < FORCE_REFRESH_TTL_SECONDS

    @classmethod
    def _mark_rate_limit(cls, environment_id: UUID) -> None:
        _rate_limit_lock[environment_id] = datetime.now(UTC)

    # ── Public API ─────────────────────────────────────────────────────

    @classmethod
    async def fetch_index(
        cls, environment: AgentEnvironment, db_session=None
    ) -> list[dict[str, Any]]:
        """Pull the skill index from the environment and cache it.

        Returns the cached entry list (JSON-safe dicts, the shape
        :meth:`SkillEntry.to_dict` produces).

        Raises:
            SkillsIndexUnavailableError: the environment is asleep
                (``env_not_running``), unreachable (``adapter_error``), or its
                ``/app/core`` predates the endpoint (``adapter_unsupported``).
                All three are normal: a suspended env and a pre-feature
                container are expected states, not failures to shout about.
                The reason on the exception is what tells a caller which of the
                three remedies to name.
        """
        from app.services.environments.environment_service import EnvironmentService

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        # Always ATTEMPT the call, and classify only on failure. The status
        # column is not a reliable pre-check: the env-start sweep runs inside
        # ``_sync_dynamic_data``, before the row is stamped ``running``, so
        # gating on it up front would make the sweep always fail on a container that
        # is demonstrably up — and would persist "the environment is asleep"
        # about an environment that just started. The sibling caches
        # (``AgentStatusService``, ``CLICommandsService``) call their adapter
        # unconditionally for exactly this reason.
        try:
            payload = await adapter.get_skills_index()
        except EndpointUnsupportedError as exc:
            # The container answered — it just has no such route, which means
            # its ``/app/core`` predates the feature. Checked BEFORE the status
            # branch on purpose: a pre-feature container that also happens to be
            # suspended still needs a rebuild, and "refresh to wake it" would
            # send the user around the same loop the wake never breaks.
            logger.debug(
                "skills_fetch_failure agent_id=%s env_id=%s reason=%s: %s",
                environment.agent_id, environment.id,
                ERROR_ADAPTER_UNSUPPORTED, exc,
            )
            cls._persist_error(environment, ERROR_ADAPTER_UNSUPPORTED, db_session)
            raise SkillsIndexUnavailableError(f"{ERROR_ADAPTER_UNSUPPORTED}: {exc}")
        except Exception as exc:
            # A failure on a sleeping env is "asleep"; anywhere else it is
            # unreachable. Both are recoverable by getting the container back —
            # unlike the unsupported case above, which no amount of restarting
            # fixes. Only the server can tell the three apart.
            reason = (
                ERROR_ENV_NOT_RUNNING
                if environment.status in SLEEPING_STATUSES
                else ERROR_ADAPTER_ERROR
            )
            logger.debug(
                "skills_fetch_failure agent_id=%s env_id=%s reason=%s: %s",
                environment.agent_id, environment.id, reason, exc,
            )
            cls._persist_error(environment, reason, db_session)
            raise SkillsIndexUnavailableError(f"{reason}: {exc}")

        if not isinstance(payload, dict):
            logger.warning(
                "skills_parse_error agent_id=%s env_id=%s (index is not an object)",
                environment.agent_id, environment.id,
            )
            cls._persist_error(environment, ERROR_PARSE_ERROR, db_session)
            raise SkillsIndexUnavailableError(ERROR_PARSE_ERROR)

        new_hash = payload.get("hash") or ""
        entries = cls._normalise_entries(payload.get("skills"))
        # In a thread: this is filesystem I/O inside an ``async def`` that runs
        # after every stream completion, cron event and start sweep. A skill
        # with no ``version:`` at all stays in the missing set forever (its
        # backfill result is ``None`` again next time), so the cost is paid on
        # every fetch — bounded by ``DEFAULT_MAX_SKILLS`` but not by anything
        # that ever goes away. The publish-side write already does this; the
        # read had no reason not to.
        await asyncio.to_thread(cls._backfill_local_versions, environment, entries)
        changed = cls._entries_differ(environment.skills_parsed, entries)

        # Short-circuit on the CONTENT, not on the reported hash. The hash
        # covers the workspace ``skills/`` tree only — deliberately, so a
        # plugin toggle does not read as a workspace edit — but the index also
        # carries every active plugin's skills. Trusting the hash would throw
        # away a freshly installed plugin's skills and, for an agent with no
        # local skills at all (a constant hash), would never show them.
        #
        # The comparison is cheap (the payload is already parsed) and what it
        # buys is the write: an unchanged index costs one HTTP round trip and
        # nothing else, which is the common case after every stream and cron.
        up_to_date = (
            not changed
            and environment.skills_parsed is not None
            and environment.skills_hash == new_hash
            and environment.skills_error is None
        )
        if up_to_date:
            cls._mark_rate_limit(environment.id)
            return entries

        now = datetime.now(UTC)

        def _persist(sess):
            env = sess.get(AgentEnvironment, environment.id)
            if env is None:
                return
            env.skills_parsed = entries
            env.skills_hash = new_hash
            env.skills_fetched_at = now
            env.skills_error = None
            sess.add(env)
            sess.commit()

        cls._with_session(_persist, db_session)
        cls._mark_rate_limit(environment.id)

        logger.info(
            "skills_fetch_success agent_id=%s env_id=%s count=%d changed=%s",
            environment.agent_id, environment.id, len(entries), changed,
        )

        # The card only needs to re-render when the LIST moved. A tree hash that
        # moved for an unrelated file (a touched README inside a skill) is not
        # worth waking every open browser tab for.
        if changed:
            cls._fire_agent_updated(environment, skill_count=len(entries))

        return entries

    @classmethod
    def get_cached(cls, environment: AgentEnvironment | None) -> list[dict[str, Any]]:
        """Return the cached index without touching the adapter."""
        if environment is None or environment.skills_parsed is None:
            return []
        return cls._normalise_entries(environment.skills_parsed)

    @classmethod
    def get_cached_entries(
        cls, environment: AgentEnvironment | None
    ) -> list[SkillEntry]:
        """Cached index as :class:`SkillEntry` objects.

        The dataclass form is what callers that need behaviour want
        (``is_valid`` / ``is_publishable``); the dict form is what callers that
        only marshal want. Both read the same cached rows.
        """
        entries: list[SkillEntry] = []
        for row in cls.get_cached(environment):
            entries.append(
                SkillEntry(
                    name=row.get("name", ""),
                    description=row.get("description") or "",
                    source=row.get("source") or "local",
                    plugin_ref=row.get("plugin_ref"),
                    path=row.get("path") or "",
                    has_scripts=bool(row.get("has_scripts")),
                    has_dashboards=bool(row.get("has_dashboards")),
                    user_invocable=row.get("user_invocable", True) is not False,
                    model_invocable=row.get("model_invocable", True) is not False,
                    size_bytes=int(row.get("size_bytes") or 0),
                    error=issue_from_dict(row.get("error")),
                    warning=issue_from_dict(row.get("warning")),
                    secret_paths=[
                        p for p in (row.get("secret_paths") or []) if isinstance(p, str)
                    ],
                    credentials=cls._normalise_credential_declarations(
                        row.get("credentials")
                    ),
                    version=coerce_version(row.get("version")),
                )
            )
        return entries

    @staticmethod
    def _normalise_credential_declarations(raw: Any) -> list[dict[str, Any]]:
        """Coerce a reported ``credentials`` list into ``{slot, type, description}``.

        Tolerant rather than validating: the parser inside the container already
        refused an invalid block (the skill carries ``invalid_credentials``), so
        this only guards the cache against a payload it cannot type. A row from a
        container built before skills declared credentials has no key → ``[]``.

        The payload comes from a container that agent code controls, so it is
        bounded here with the parser's own limits: at most
        ``MAX_SKILL_CREDENTIALS`` items; an item with an over-long slot (a
        truncated slot would be a different slot) or an unknown type is
        dropped; a description is truncated.
        """
        if not isinstance(raw, list):
            return []
        declarations: list[dict[str, Any]] = []
        for item in raw[:MAX_SKILL_CREDENTIALS]:
            if not isinstance(item, dict):
                continue
            slot, credential_type = item.get("slot"), item.get("type")
            if not isinstance(slot, str) or not isinstance(credential_type, str):
                continue
            if len(slot) > MAX_SLOT_LENGTH or credential_type not in SKILL_CREDENTIAL_TYPES:
                continue
            description = item.get("description")
            declarations.append(
                {
                    "slot": slot,
                    "type": credential_type,
                    "description": description[:MAX_DESCRIPTION_LENGTH]
                    if isinstance(description, str)
                    else None,
                }
            )
        return declarations

    @staticmethod
    def issue_to_public(issue) -> "SkillIssuePublic | None":
        """Project one flagged condition, or ``None``."""
        from app.models.agents.agent_skills import SkillIssuePublic

        if issue is None:
            return None
        return SkillIssuePublic(
            code=issue.code, message=issue.message, paths=list(issue.paths)
        )

    @classmethod
    def entry_to_public(
        cls, entry: SkillEntry, *, can_publish: bool
    ) -> "SkillEntryPublic":
        """Project one cached entry, resolving its per-entry publish capability.

        ``can_publish`` here is the agent-level capability; the entry adds the
        condition that the skill itself is publishable — clean, and locally
        owned (a plugin's skill belongs to the plugin's publisher, not to this
        agent).

        Lives on the service rather than in a route because two surfaces
        project the same rows — the skills index and the addons projection —
        and a second copy of this mapping is how one of them would end up
        offering a verb the other refuses.
        """
        from app.models.agents.agent_skills import (
            SkillCredentialDeclarationPublic,
            SkillEntryPublic,
        )

        return SkillEntryPublic(
            name=entry.name,
            description=entry.description,
            source=entry.source,
            plugin_ref=entry.plugin_ref,
            path=entry.path,
            has_scripts=entry.has_scripts,
            has_dashboards=entry.has_dashboards,
            user_invocable=entry.user_invocable,
            model_invocable=entry.model_invocable,
            size_bytes=entry.size_bytes,
            version=entry.version,
            error=cls.issue_to_public(entry.error),
            warning=cls.issue_to_public(entry.warning),
            secret_paths=list(entry.secret_paths),
            credentials=[
                SkillCredentialDeclarationPublic(
                    slot=declaration["slot"],
                    type=declaration["type"],
                    description=declaration.get("description"),
                )
                for declaration in entry.credentials
            ],
            can_publish=(
                can_publish and entry.source == "local" and entry.is_publishable
            ),
        )

    # ── Refresh triggers ───────────────────────────────────────────────

    @classmethod
    async def refresh_after_action(
        cls,
        environment: AgentEnvironment,
        db_session=None,
        force: bool = False,
    ) -> None:
        """Pull the index after the backend finished work inside the agent-env.

        Skipped while the per-env rate-limit window is open unless ``force``
        (the start sweep, an explicit user refresh, or a watcher signal that
        named ``skills/`` — all three are direct evidence rather than a guess).

        Best-effort: never raises.
        """
        if not force and cls.is_rate_limited(environment.id):
            return
        try:
            await cls.fetch_index(environment, db_session=db_session)
        except SkillsIndexUnavailableError:
            pass  # env not running / pre-feature container — both normal
        except Exception as exc:
            logger.debug(
                "skills refresh_after_action failed for env %s: %s",
                environment.id, exc,
            )

    @classmethod
    async def force_refresh(
        cls,
        environment: AgentEnvironment,
        agent=None,
        db_session=None,
    ) -> list[dict[str, Any]]:
        """User-initiated refresh: wake a suspended env, then pull the index.

        The single entrypoint behind the card's Refresh button, the
        ``POST /agents/{id}/skills/refresh`` route and the ``/skills`` command's
        stale-cache path — the same posture ``/agent-status`` takes, so a
        sleeping agent answers a refresh instead of serving a stale list
        forever.

        Never raises: an unreachable environment falls back to the cached rows,
        with the reason recorded in ``skills_error`` for the caller to render.
        """
        from app.services.agents.environment_resolver import (
            wake_suspended_environment,
        )

        if agent is None:
            agent = cls._load_agent(environment.agent_id, db_session)
        await wake_suspended_environment(environment, agent, log_prefix="agent_skills")

        try:
            return await cls.fetch_index(environment, db_session=db_session)
        except SkillsIndexUnavailableError:
            return cls.get_cached(environment)
        except Exception as exc:
            logger.debug("skills force_refresh failed for env %s: %s", environment.id, exc)
            return cls.get_cached(environment)

    @classmethod
    def find_cached_skill(
        cls, environment: AgentEnvironment, name: str
    ) -> SkillEntry | None:
        """The cached entry for ``name``, or ``None``.

        When a local and a plugin skill share a name (the ``shadowed``
        warning) the local one wins — the index sorts ``local`` before
        ``plugin``, and the agent's own copy is the one its owner came to read.
        """
        return next(
            (e for e in cls.get_cached_entries(environment) if e.name == name),
            None,
        )

    @classmethod
    async def read_skill_content(
        cls, environment: AgentEnvironment, name: str, agent: Agent | None = None
    ) -> tuple[str, str, bool] | None:
        """Read one skill's ``SKILL.md`` — ``(path, text, truncated)``.

        Returns ``None`` when the cache holds no such skill, or when the file
        is gone from a workspace the cache still describes. The caller tells the
        two apart with :meth:`find_cached_skill` — both are 404s, but only one
        of them means "your cache is stale".

        The path comes from the cached index, never from ``name``: that is what
        keeps this from being a workspace file-read endpoint wearing a skill's
        name, and it is what makes a plugin's skill resolve inside the plugin
        folder instead of the agent's own.

        Opening a skill is a user asking, so it takes the :meth:`force_refresh`
        posture: a suspended environment is woken before the read rather than
        reported as unavailable. The wake comes after the cache lookup, so a
        name that is not a skill never starts a container.

        Raises:
            SkillsIndexUnavailableError: the environment could not be reached.
        """
        entry = cls.find_cached_skill(environment, name)
        if entry is None or not entry.path:
            return None

        from app.services.agents.environment_resolver import (
            wake_suspended_environment,
        )
        from app.services.environments.environment_service import EnvironmentService

        if agent is None:
            agent = cls._load_agent(environment.agent_id)
        await wake_suspended_environment(environment, agent, log_prefix="agent_skills")

        rel_path = f"{entry.path}/SKILL.md"
        adapter = EnvironmentService.get_lifecycle_manager().get_adapter(environment)
        try:
            meta, stream = await adapter.fetch_workspace_item_with_meta(rel_path)
        except Exception as exc:
            raise SkillsIndexUnavailableError(f"{ERROR_ADAPTER_ERROR}: {exc}")

        if not meta.exists:
            return None

        text, truncated = await cls._consume_stream(stream, MAX_CONTENT_BYTES)
        return rel_path, text, truncated

    @classmethod
    def list_skill_files(
        cls, environment: AgentEnvironment, name: str
    ) -> tuple[str, list[SkillRevisionFilePublic], int, int] | None:
        """What one skill folder carries — ``(path, files, count, total bytes)``.

        ``files`` is capped at the catalog's ``MAX_LISTED_FILES``; the count and
        the byte total always describe the whole folder. The same contract and
        row type as ``SkillCatalogService.list_revision_files``, so one Sheet
        renders a published revision and a workspace folder alike.

        Read **host-side** off the workspace bind mount, as the publish path and
        :meth:`_backfill_local_versions` read it: names and sizes need no
        container, so a suspended agent answers without being woken. Plugin
        folders live under the same mount, so a plugin's skill resolves too.

        The folder comes from the cached index, never from ``name`` — the rule
        :meth:`read_skill_content` follows — and that index is written by a
        process inside the agent's own container, so it is untrusted twice over:
        the path must have one of the two shapes a skill folder has
        (:func:`_is_skill_folder_path`), or ``"."`` would list the whole
        workspace; and the folder is opened and walked by file descriptor
        (:func:`_walk_skill_folder`), so a symlink swapped in after that check
        is refused instead of followed off the mount. The exclusions are the
        index walk's own (``SKIP_DIR_NAMES``, symlinks at every depth), so the
        dialog's Size fact and this total describe the same files.

        Returns ``None`` when the cache holds no such skill, or when its folder
        is gone, is not a skill folder, or is reached through a symlink; the
        caller tells the first case from the rest with :meth:`find_cached_skill`.

        Raises:
            SkillsIndexUnavailableError: the workspace is not on disk at all —
                an environment that has never started.
        """
        from app.models.skills.schemas import SkillRevisionFilePublic
        from app.services.skills.skill_catalog_service import MAX_LISTED_FILES

        entry = cls.find_cached_skill(environment, name)
        if entry is None or not _is_skill_folder_path(entry.path):
            return None

        workspace = (
            Path(settings.ENV_INSTANCES_DIR) / str(environment.id) / WORKSPACE_ROOT_REL
        )
        if not workspace.is_dir():
            raise SkillsIndexUnavailableError("workspace_unavailable")

        files: list[SkillRevisionFilePublic] = []
        count = 0
        total_bytes = 0
        try:
            for relative, stat_result in _walk_skill_folder(workspace, entry.path):
                count += 1
                total_bytes += stat_result.st_size
                if len(files) < MAX_LISTED_FILES:
                    files.append(
                        SkillRevisionFilePublic(
                            path=relative,
                            size_bytes=stat_result.st_size,
                            is_executable=bool(stat_result.st_mode & 0o111),
                        )
                    )
        except OSError:
            # A component of the folder is missing or is not a real directory.
            return None
        return entry.path, files, count, total_bytes

    @staticmethod
    async def _consume_stream(stream, max_bytes: int) -> tuple[str, bool]:
        """Read a byte stream into text, stopping at ``max_bytes``.

        Decoding with ``errors="replace"`` rather than raising: a SKILL.md with
        one bad byte is still worth showing, and a truncation cut can land
        mid-codepoint by construction.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream:
            total += len(chunk)
            if total > max_bytes:
                chunks.append(chunk[: len(chunk) - (total - max_bytes)])
                return b"".join(chunks).decode("utf-8", errors="replace"), True
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace"), False

    @classmethod
    def _load_agent(cls, agent_id, db_session=None):
        """Load the owning Agent, reusing the caller's session when there is one."""
        if agent_id is None:
            return None
        from app.models.agents.agent import Agent

        if db_session is not None:
            return db_session.get(Agent, agent_id)
        from app.core.db import create_session

        with create_session() as session:
            return session.get(Agent, agent_id)

    @classmethod
    async def handle_post_action_event(cls, event_data: dict) -> None:
        """Refresh the cache after any backend-triggered agent-env action.

        Registered against ``ENVIRONMENT_ACTIVATED``, ``STREAM_COMPLETED`` /
        ``STREAM_ERROR``, the ``CRON_*`` family and ``WORKSPACE_FILES_CHANGED``
        — the same set every pull-only cache uses, derived from the synced-file
        registry in ``app/main.py``.

        A ``WORKSPACE_FILES_CHANGED`` naming ``skills/`` is direct evidence the
        cache is stale, so it bypasses the rate limit; every other trigger is
        speculative and does not.
        """
        try:
            meta = event_data.get("meta", {}) or {}
            environment_id = meta.get("environment_id")
            if not environment_id:
                return
            changed_files = meta.get("changed_files") or []
            force = SKILLS_DIR_PATH in changed_files

            from app.core.db import create_session

            with create_session() as session:
                env = session.get(AgentEnvironment, UUID(str(environment_id)))
                if env is None:
                    return
                await cls.refresh_after_action(env, db_session=session, force=force)
        except Exception as exc:
            logger.debug("skills handle_post_action_event swallowed: %s", exc)

    # ── Private helpers ────────────────────────────────────────────────

    @staticmethod
    def _with_session(operation, db_session) -> None:
        """Run ``operation(session)`` on the caller's session or a fresh one."""
        if db_session is not None:
            operation(db_session)
            return
        from app.core.db import create_session

        with create_session() as session:
            operation(session)

    @staticmethod
    def _backfill_local_versions(
        environment: AgentEnvironment, entries: list[dict[str, Any]]
    ) -> None:
        """Fill in ``version`` for local skills the container did not report one for.

        The index is built **inside** the container by env-core's vendored copy
        of ``skill_manifest.py``, and ``app/core/`` is copied out of the
        template at environment *creation* — not at every start. So every
        environment created before skills carried a version reports rows with
        no ``version`` key and would go on reporting none forever, however many
        times its author edits ``SKILL.md`` and presses Refresh. The row would
        say "No version" while the publish path — which reads the same file
        from the host — published one, which is one surface giving two answers.

        The host can read the file itself: a local skill's folder is bind-mounted
        at ``<ENV_INSTANCES_DIR>/<env id>/app/workspace/skills/<name>/``, the
        same path the catalog publishes from. So this reads the frontmatter
        there for exactly the rows that are missing the field.

        A **backfill, not an override**: a container that reports a version is
        believed, because it is the thing that actually loaded the skill. And
        best-effort throughout — a workspace that is not on disk (an environment
        that has never started) simply leaves the rows as they came, and a
        rebuilt container stops needing this at all.
        """
        missing = [
            row
            for row in entries
            if row.get("source") == "local"
            and not row.get("version")
            # Guards the path join below as much as it validates the row: the
            # name becomes a path segment, and the index is written by a
            # process running inside the agent's own container.
            and SKILL_NAME_RE.match(row.get("name") or "")
        ]
        if not missing:
            return

        try:
            workspace = (
                Path(settings.ENV_INSTANCES_DIR)
                / str(environment.id)
                / WORKSPACE_ROOT_REL
                / "skills"
            )
            if not workspace.is_dir():
                return
            for row in missing:
                skill_dir = workspace / row["name"]
                # Both links, not just the file: an agent controls its own
                # workspace, and a symlinked *directory* would let it surface
                # another environment's `version:` on its own row. Both sibling
                # readers of this tree check the directory
                # (``SkillCatalogService._resolve_skill_dir``,
                # ``parse_skill_dir``); this one was the odd read out.
                if skill_dir.is_symlink() or not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if skill_md.is_symlink() or not skill_md.is_file():
                    continue
                try:
                    frontmatter, _ = parse_frontmatter(
                        skill_md.read_text(encoding="utf-8-sig")
                    )
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                row["version"] = coerce_version(frontmatter.get("version"))
        except Exception as exc:  # never break an index over a file read
            logger.debug(
                "skills_version_backfill_failed env_id=%s: %s", environment.id, exc
            )

    @classmethod
    def _normalise_entries(cls, raw: Any) -> list[dict[str, Any]]:
        """Coerce a reported index into the cached row shape.

        Defensive on purpose: the payload comes from a container the agent's
        own code runs in, so every field is validated rather than trusted, and
        an entry without a name is dropped instead of poisoning the card.
        """
        if not isinstance(raw, list):
            return []
        entries: list[dict[str, Any]] = []
        for item in raw[:MAX_CACHED_SKILLS]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            error = issue_from_dict(item.get("error"))
            warning = issue_from_dict(item.get("warning"))
            secret_paths = item.get("secret_paths")
            entries.append(
                {
                    "name": name,
                    "description": str(item.get("description") or ""),
                    "source": str(item.get("source") or "local"),
                    "plugin_ref": item.get("plugin_ref")
                    if isinstance(item.get("plugin_ref"), str)
                    else None,
                    "path": str(item.get("path") or f"skills/{name}"),
                    "has_scripts": bool(item.get("has_scripts")),
                    "has_dashboards": bool(item.get("has_dashboards")),
                    "user_invocable": item.get("user_invocable", True) is not False,
                    "model_invocable": item.get("model_invocable", True) is not False,
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "error": error.to_dict() if error else None,
                    "warning": warning.to_dict() if warning else None,
                    "secret_paths": [
                        p for p in secret_paths if isinstance(p, str)
                    ] if isinstance(secret_paths, list) else [],
                    # Absent from every container built before skills carried a
                    # version, which is why it is normalised rather than read:
                    # such an environment reports rows without the key and must
                    # cache `None` rather than fail the whole index.
                    "version": coerce_version(item.get("version")),
                    # Absent from every container built before skills could
                    # declare credentials; normalised to `[]` for the same
                    # reason as `version` above.
                    "credentials": cls._normalise_credential_declarations(
                        item.get("credentials")
                    ),
                }
            )
        return entries

    @staticmethod
    def _entries_differ(cached: Any, fresh: list[dict[str, Any]]) -> bool:
        """True when the cached list is not the same list as ``fresh``."""
        if cached is None:
            return bool(fresh)
        if not isinstance(cached, list) or len(cached) != len(fresh):
            return True
        return cached != fresh

    @classmethod
    def _persist_error(
        cls,
        environment: AgentEnvironment,
        error_reason: str,
        db_session=None,
    ) -> None:
        """Record why the index could not be read, keeping the cached rows.

        The rows stay: a suspended environment still has the skills it had, and
        blanking the card on a sleeping agent would be a worse answer than
        showing what we know with a banner over it.
        """
        def _do_persist(sess):
            env = sess.get(AgentEnvironment, environment.id)
            if env is None:
                return
            env.skills_error = error_reason
            sess.add(env)
            sess.commit()

        try:
            cls._with_session(_do_persist, db_session)
        except Exception as exc:
            logger.debug("skills _persist_error failed: %s", exc)

    @classmethod
    def _fire_agent_updated(
        cls, environment: AgentEnvironment, skill_count: int
    ) -> None:
        """Emit ``AGENT_UPDATED`` so the owner's open agent page re-renders.

        Best-effort and fire-and-forget: the cache is already written, and a
        missed notification costs one manual refresh.
        """
        try:
            from app.core.db import create_session
            from app.models.agents.agent import Agent
            from app.models.events.event import EventType
            from app.services.events.event_service import event_service

            with create_session() as session:
                agent = session.get(Agent, environment.agent_id)
                owner_id = agent.owner_id if agent else None

            if owner_id is None:
                return

            async def _emit() -> None:
                await event_service.emit_event(
                    event_type=EventType.AGENT_UPDATED,
                    model_id=environment.agent_id,
                    user_id=owner_id,
                    meta={
                        "agent_id": str(environment.agent_id),
                        "environment_id": str(environment.id),
                        "changed_fields": ["skills"],
                        "skill_count": skill_count,
                    },
                )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_emit())
            except RuntimeError:
                pass  # no running loop (sync context) — nothing to notify
        except Exception as exc:
            logger.debug("Failed to emit AGENT_UPDATED for a skills change: %s", exc)
