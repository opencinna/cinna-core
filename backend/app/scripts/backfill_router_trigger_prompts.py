"""Phase 8 — Backfill router trigger prompts + auto-managed App MCP routes.

Wires existing **foreign bundle installs** (``Agent.is_publisher_install=False``
AND ``bundle_uuid IS NOT NULL``) into the App MCP router so they're
reachable from external clients (Claude Desktop, etc.) without a
republish. For every eligible install that doesn't already have an
auto-managed ``AppAgentRoute``:

  1. Generate a ``router_trigger_prompt`` from ``Agent.description`` via
     the existing :func:`generate_router_trigger_prompt` AI function.
  2. Persist it on ``Agent.router_trigger_prompt``.
  3. Create an ``AppAgentRoute`` (``is_auto_managed=True``,
     ``session_mode="conversation"``, ``channel_app_mcp=True``,
     ``is_active=True``, ``name=agent.name``) + a self-assignment for
     the installer (``user_id=agent.owner_id``, ``is_enabled=True``).

Owned agents that are NOT bundle installs (``bundle_uuid IS NULL``) are
**intentionally skipped**. The owner manages App MCP exposure for their
own agents directly via the agent's Integrations tab; auto-routing is
reserved for the install-from-catalog flow.

Schema migrations stay in Alembic. This script is the data half of
Phase 8 because the AI function call doesn't belong inside a migration
(slow, no per-row transaction semantics, complicates downgrades).

Idempotent:
  - Skips installs whose ``description`` is empty (no generator input).
  - Skips installs where any ``AppAgentRoute`` with
    ``is_auto_managed=True`` already exists for that agent owned by the
    installer.
  - Skips publisher installs (``is_publisher_install=True``) — those
    don't need a router route until they're installed elsewhere.
  - Skips owned non-bundle agents (``bundle_uuid IS NULL``) — the owner
    controls App MCP exposure manually for these.

Failure handling:
  - AI-function failures for a given agent are logged at WARNING and
    the script continues with the next agent.
  - Any other unexpected exception per agent is caught, logged, and
    the script continues.

Run with::

    docker compose exec backend python -m app.scripts.backfill_router_trigger_prompts

Optional ``--dry-run`` prints what would change without writing.
"""
import argparse
import logging
import sys

from sqlmodel import Session, select

from app.agents import generate_router_trigger_prompt
from app.core.db import engine
from app.models.agents.agent import Agent
from app.models.app_mcp.app_agent_route import (
    AppAgentRoute,
    AppAgentRouteCreate,
)
from app.models.users.user import User
from app.services.app_mcp.app_agent_route_service import AppAgentRouteService


logger = logging.getLogger(__name__)


def _has_auto_managed_route(session: Session, agent_id) -> bool:
    """True if the agent already owns an auto-managed App MCP route."""
    stmt = select(AppAgentRoute).where(
        AppAgentRoute.agent_id == agent_id,
        AppAgentRoute.is_auto_managed == True,  # noqa: E712
    )
    return session.exec(stmt).first() is not None


def backfill(session: Session, dry_run: bool = False) -> dict:
    """Run the backfill against an open DB session.

    Returns a dict of counters useful for logging / smoke checks.
    """
    counters = {
        "scanned": 0,
        "skipped_no_description": 0,
        "skipped_already_routed": 0,
        "skipped_generation_failed": 0,
        "skipped_missing_owner": 0,
        "generated_and_routed": 0,
        "errors": 0,
    }

    # Foreign bundle installs only. Owned agents that aren't installs
    # (``bundle_uuid IS NULL``) are managed by their owner via the
    # Integrations tab — never auto-routed.
    stmt = select(Agent).where(
        Agent.is_publisher_install == False,  # noqa: E712
        Agent.bundle_uuid.is_not(None),
    )
    agents = session.exec(stmt).all()

    for agent in agents:
        counters["scanned"] += 1
        try:
            description = (agent.description or "").strip()
            if not description:
                counters["skipped_no_description"] += 1
                continue
            if _has_auto_managed_route(session, agent.id):
                counters["skipped_already_routed"] += 1
                continue

            # Generator may return an empty string only when description
            # is empty (already filtered above). It returns a fallback
            # prefix + truncated description on transient failures, so
            # we still get a usable prompt. Empty result here is
            # treated as a generation failure.
            trigger_prompt = generate_router_trigger_prompt(
                agent_name=agent.name,
                description=description,
            )
            trigger_prompt = (trigger_prompt or "").strip()
            if not trigger_prompt:
                logger.warning(
                    "Generation returned empty prompt for agent %s (%s); skipping",
                    agent.id, agent.name,
                )
                counters["skipped_generation_failed"] += 1
                continue

            owner = session.get(User, agent.owner_id)
            if owner is None:
                logger.warning(
                    "Agent %s has no resolvable owner %s; skipping",
                    agent.id, agent.owner_id,
                )
                counters["skipped_missing_owner"] += 1
                continue

            if dry_run:
                logger.info(
                    "[dry-run] Would set router_trigger_prompt on agent %s "
                    "(%s, owner=%s) and create auto-managed AppAgentRoute. "
                    "Prompt preview: %r",
                    agent.id, agent.name, owner.email, trigger_prompt[:120],
                )
                counters["generated_and_routed"] += 1
                continue

            agent.router_trigger_prompt = trigger_prompt
            session.add(agent)
            session.commit()
            session.refresh(agent)

            payload = AppAgentRouteCreate(
                name=agent.name,
                agent_id=agent.id,
                session_mode="conversation",
                trigger_prompt=trigger_prompt,
                channel_app_mcp=True,
                is_active=True,
                auto_enable_for_users=False,
                activate_for_myself=True,
            )
            AppAgentRouteService.create_route(
                db_session=session,
                data=payload,
                current_user=owner,
                auto_managed=True,
            )
            counters["generated_and_routed"] += 1
            logger.info(
                "Backfilled agent %s (%s) for owner %s",
                agent.id, agent.name, owner.email,
            )
        except Exception as exc:  # noqa: BLE001 — defensive: log + continue
            counters["errors"] += 1
            logger.warning(
                "Backfill failed for agent %s (%s): %s",
                getattr(agent, "id", "?"),
                getattr(agent, "name", "?"),
                exc,
            )
            # Roll back any partial state from this iteration so the next
            # agent gets a clean session.
            try:
                session.rollback()
            except Exception:
                pass

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would change without writing anything to the DB.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    logger.info(
        "Starting router-trigger-prompt backfill (dry_run=%s)", args.dry_run,
    )
    with Session(engine) as session:
        counters = backfill(session, dry_run=args.dry_run)

    logger.info("Backfill summary: %s", counters)
    return 0


if __name__ == "__main__":
    sys.exit(main())
