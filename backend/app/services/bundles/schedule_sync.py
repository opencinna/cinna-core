"""Schedule snapshot + sync helpers for bundle propagation.

A publisher ships their ``AgentSchedule`` rows as part of a bundle revision
(``revision.schedules``). On install the consumer receives those schedules
pre-populated with the published enabled/disabled state; on apply-update the
consumer's schedules are merged so a user's enable/disable survives a
behaviorally-unchanged schedule while changed/added/removed schedules are
synced from the new revision.

Identity ("same scheduler" across revisions) uses a **behavioral
signature** — ``(schedule_type, cron_string, command, prompt)``.
``name``/``description`` are cosmetic and excluded from identity: a rename or
description tweak keeps the user's toggle; a cron/command/prompt/type change
is treated as a different scheduler (reinstalled).

A consumer install's schedules are entirely bundle-owned — consumers can't
create their own — so the full schedule set on a foreign install is
bundle-managed and the merge can delete any row whose signature is gone from
the new revision.

``next_execution`` / ``last_execution`` are never snapshotted; ``next_execution``
is (re)computed from the revision's UTC cron via
``AgentSchedulerService.calculate_next_execution``.
"""
import logging
import uuid

from sqlmodel import Session

from app.models.agents.agent import Agent
from app.models.agents.agent_schedule import AgentSchedule
from app.models.bundles.agent_bundle_revision import AgentBundleRevision

logger = logging.getLogger(__name__)


# Behavioral identity fields snapshotted from each schedule. ``name`` and
# ``description`` are cosmetic — refreshed on a behaviorally-unchanged match
# but not part of the signature.
_SNAPSHOT_FIELDS = (
    "name",
    "cron_string",
    "description",
    "prompt",
    "schedule_type",
    "command",
    "enabled",
)


def snapshot_schedules(schedules: list[AgentSchedule]) -> list[dict]:
    """Project ``AgentSchedule`` rows into the revision snapshot shape.

    Returns a list of ``{name, cron_string, description, prompt,
    schedule_type, command, enabled}`` dicts. ``next_execution`` /
    ``last_execution`` are intentionally omitted — they are per-install
    runtime state, recomputed on materialisation.
    """
    snapshot: list[dict] = []
    for sched in schedules:
        snapshot.append(
            {
                "name": sched.name,
                "cron_string": sched.cron_string,
                "description": sched.description,
                "prompt": sched.prompt,
                "schedule_type": sched.schedule_type,
                "command": sched.command,
                "enabled": bool(sched.enabled),
            }
        )
    return snapshot


def sig(source: object) -> tuple:
    """Return the behavioral signature of a schedule row or snapshot dict.

    Signature = ``(schedule_type, cron_string, command, prompt)``. Works on
    both an :class:`AgentSchedule` row and a snapshot ``dict`` so the merge
    can compare existing rows against new revision definitions uniformly.
    """
    if isinstance(source, dict):
        schedule_type = source.get("schedule_type") or "static_prompt"
        cron_string = source.get("cron_string")
        command = source.get("command")
        prompt = source.get("prompt")
    else:
        schedule_type = getattr(source, "schedule_type", None) or "static_prompt"
        cron_string = getattr(source, "cron_string", None)
        command = getattr(source, "command", None)
        prompt = getattr(source, "prompt", None)
    return (schedule_type, cron_string, command, prompt)


def _create_from_def(
    session: Session, install: Agent, definition: dict
) -> AgentSchedule:
    """Create one ``AgentSchedule`` row from a revision snapshot dict.

    ``cron_string`` in the snapshot is already UTC (it was stored UTC on the
    publisher row), so ``next_execution`` is computed directly via
    ``calculate_next_execution`` — no timezone conversion.
    """
    from app.services.agents.agent_scheduler_service import AgentSchedulerService

    cron_string = definition.get("cron_string") or ""
    next_execution = AgentSchedulerService.calculate_next_execution(cron_string)

    schedule = AgentSchedule(
        agent_id=install.id,
        name=definition.get("name") or "Scheduled run",
        cron_string=cron_string,
        description=definition.get("description") or "",
        prompt=definition.get("prompt"),
        schedule_type=definition.get("schedule_type") or "static_prompt",
        command=definition.get("command"),
        enabled=bool(definition.get("enabled", True)),
        next_execution=next_execution,
    )
    session.add(schedule)
    return schedule


def _group_by_sig(definitions: list) -> dict[tuple, dict]:
    """Group revision schedule definitions by behavioral signature.

    Duplicate signatures within one revision are assumed unique; on a
    collision the later definition wins (documented limitation). Non-dict
    entries are skipped defensively.
    """
    grouped: dict[tuple, dict] = {}
    for definition in definitions or []:
        if not isinstance(definition, dict):
            continue
        grouped[sig(definition)] = definition
    return grouped


def materialise(
    session: Session, install: Agent, revision: AgentBundleRevision
) -> int:
    """Create ``AgentSchedule`` rows on ``install`` from ``revision.schedules``.

    Used at install time. Creates one row per snapshotted schedule with the
    published ``enabled`` state and a freshly computed ``next_execution``.
    Returns the number of schedules created.

    Caller is responsible for committing the session — this method only
    stages the rows (mirrors the create branch of :func:`merge`).
    """
    created = 0
    for definition in revision.schedules or []:
        if not isinstance(definition, dict):
            continue
        _create_from_def(session, install, definition)
        created += 1
    return created


def merge(
    session: Session, install: Agent, revision: AgentBundleRevision
) -> None:
    """Reconcile ``install``'s schedules against ``revision.schedules``.

    Algorithm (consumer installs are entirely bundle-owned):

    - For each existing row whose signature is still present in the new
      revision: keep the row (preserve ``enabled``, ``next_execution``,
      ``last_execution``, and logs) but refresh the cosmetic ``name`` +
      ``description`` from the new definition. That definition is then
      consumed.
    - Existing rows whose signature is gone (changed or removed by the
      publisher) are deleted.
    - Remaining (unconsumed) revision definitions are added or changed
      schedules → create new rows with the published ``enabled`` state and a
      computed ``next_execution``.

    Commits at the end.
    """
    from app.services.agents.agent_scheduler_service import AgentSchedulerService

    new_by_sig = _group_by_sig(revision.schedules)

    existing = AgentSchedulerService.get_agent_schedules(session, install.id)

    for row in existing:
        signature = sig(row)
        definition = new_by_sig.pop(signature, None)
        if definition is not None:
            # Behaviorally unchanged — keep the row (and its toggle / logs),
            # refresh only the cosmetic fields.
            new_name = definition.get("name")
            new_description = definition.get("description")
            changed = False
            if new_name is not None and row.name != new_name:
                row.name = new_name
                changed = True
            if new_description is not None and row.description != new_description:
                row.description = new_description
                changed = True
            if changed:
                session.add(row)
        else:
            # Signature gone from the new revision → publisher changed or
            # removed this schedule. Delete it.
            session.delete(row)

    # Remaining definitions are new or behaviorally-changed schedules.
    for definition in new_by_sig.values():
        _create_from_def(session, install, definition)

    session.commit()
