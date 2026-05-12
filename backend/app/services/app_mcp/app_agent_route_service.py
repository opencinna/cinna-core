"""
App Agent Route Service — business logic for application agent routes.

Routes can be created by any user for their own agents, or by superusers for any agent.
"""
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, UTC

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession, select

from app.models import Agent, User
from app.models.app_mcp.app_agent_route import (
    AppAgentRoute,
    AppAgentRouteAssignment,
    AppAgentRouteCreate,
    AppAgentRouteUpdate,
    AppAgentRoutePublic,
    AppAgentRouteAssignmentPublic,
    RouteConflictMatch,
    RouteConflictResponse,
    UserAppAgentRoute,
    UserAppAgentRouteCreate,
    UserAppAgentRouteUpdate,
    UserAppAgentRoutePublic,
    SharedRoutePublic,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EffectiveRoute:
    """Unified representation of an active route (assigned, personal, or identity)."""

    route_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    session_mode: str
    trigger_prompt: str
    message_patterns: str | None
    source: str  # "admin" | "user" | "identity"
    # Identity-specific fields (only set when source == "identity")
    identity_owner_id: uuid.UUID | None = None
    identity_owner_name: str | None = None
    identity_owner_email: str | None = None
    prompt_examples: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_agent_name(db_session: DBSession, agent_id: uuid.UUID) -> str:
    agent = db_session.get(Agent, agent_id)
    return agent.name if agent else ""


def _route_to_public(
    db_session: DBSession,
    route: AppAgentRoute,
    include_assignments: bool = True,
) -> AppAgentRoutePublic:
    agent = db_session.get(Agent, route.agent_id)
    agent_name = agent.name if agent else ""

    agent_owner_name = ""
    agent_owner_email = ""
    if agent:
        owner = db_session.get(User, agent.owner_id)
        if owner:
            agent_owner_name = owner.full_name or ""
            agent_owner_email = owner.email or ""

    assignments: list[AppAgentRouteAssignmentPublic] = []
    if include_assignments:
        stmt = select(AppAgentRouteAssignment).where(
            AppAgentRouteAssignment.route_id == route.id
        )
        assignments = [
            AppAgentRouteAssignmentPublic(
                id=a.id,
                route_id=a.route_id,
                user_id=a.user_id,
                is_enabled=a.is_enabled,
                created_at=a.created_at,
            )
            for a in db_session.exec(stmt).all()
        ]
    return AppAgentRoutePublic(
        id=route.id,
        name=route.name,
        agent_id=route.agent_id,
        agent_name=agent_name,
        session_mode=route.session_mode,
        trigger_prompt=route.trigger_prompt,
        message_patterns=route.message_patterns,
        prompt_examples=route.prompt_examples,
        channel_app_mcp=route.channel_app_mcp,
        is_active=route.is_active,
        auto_enable_for_users=route.auto_enable_for_users,
        is_auto_managed=route.is_auto_managed,
        agent_owner_name=agent_owner_name,
        agent_owner_email=agent_owner_email,
        created_by=route.created_by,
        created_at=route.created_at,
        updated_at=route.updated_at,
        assignments=assignments,
    )


def _user_route_to_public(
    db_session: DBSession,
    route: UserAppAgentRoute,
) -> UserAppAgentRoutePublic:
    agent_name = _get_agent_name(db_session, route.agent_id)
    return UserAppAgentRoutePublic(
        id=route.id,
        user_id=route.user_id,
        agent_id=route.agent_id,
        agent_name=agent_name,
        session_mode=route.session_mode,
        trigger_prompt=route.trigger_prompt,
        message_patterns=route.message_patterns,
        channel_app_mcp=route.channel_app_mcp,
        is_active=route.is_active,
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


# ---------------------------------------------------------------------------
# AppAgentRouteService
# ---------------------------------------------------------------------------


class AppAgentRouteService:
    """Business logic for Application Agent Routes.

    Any user can create/manage routes for their own agents.
    Superusers can create/manage routes for any agent.
    """

    @staticmethod
    def create_route(
        db_session: DBSession,
        data: AppAgentRouteCreate,
        current_user: User,
        *,
        auto_managed: bool = False,
    ) -> AppAgentRoutePublic:
        """Create a new route with optional user assignments.

        ``auto_managed`` is an internal flag flipped on by
        ``InstallService`` and the Phase 8 backfill script when they
        mint a bundle-managed route. It is intentionally a service-level
        kwarg rather than a field on ``AppAgentRouteCreate`` so the
        public POST cannot mint bundle-managed routes (which the
        apply-update path would then overwrite on the next revision
        push).

        Raises ValueError for permission violations.
        """
        agent = db_session.get(Agent, data.agent_id)
        if not agent:
            raise ValueError(f"Agent {data.agent_id} not found")

        # Ownership check: non-superusers can only create routes for their own agents
        if not current_user.is_superuser and agent.owner_id != current_user.id:
            raise ValueError("You can only create routes for your own agents")

        # Only superusers may set auto_enable_for_users=True
        if data.auto_enable_for_users and not current_user.is_superuser:
            raise ValueError("Only administrators can auto-enable routes for users")

        route = AppAgentRoute(
            name=data.name,
            agent_id=data.agent_id,
            session_mode=data.session_mode,
            trigger_prompt=data.trigger_prompt,
            message_patterns=data.message_patterns,
            prompt_examples=data.prompt_examples,
            channel_app_mcp=data.channel_app_mcp,
            is_active=data.is_active,
            auto_enable_for_users=data.auto_enable_for_users,
            is_auto_managed=auto_managed,
            created_by=current_user.id,
        )
        db_session.add(route)
        db_session.flush()  # get route.id

        # Auto-add creator if activate_for_myself is enabled
        all_user_ids = list(data.assigned_user_ids)
        if data.activate_for_myself and current_user.id not in all_user_ids:
            all_user_ids.insert(0, current_user.id)

        for user_id in all_user_ids:
            is_creator = user_id == current_user.id
            assignment = AppAgentRouteAssignment(
                route_id=route.id,
                user_id=user_id,
                is_enabled=True if is_creator else data.auto_enable_for_users,
            )
            db_session.add(assignment)

        db_session.commit()
        db_session.refresh(route)
        return _route_to_public(db_session, route)

    @staticmethod
    def list_routes(db_session: DBSession) -> list[AppAgentRoutePublic]:
        """List all routes (superuser only)."""
        routes = db_session.exec(select(AppAgentRoute)).all()
        return [_route_to_public(db_session, r) for r in routes]

    @staticmethod
    def list_routes_for_agent(
        db_session: DBSession,
        agent_id: uuid.UUID,
        current_user: User,
    ) -> list[AppAgentRoutePublic]:
        """List routes for a specific agent.

        Non-superusers see only routes they created.
        Superusers see all routes for the agent.
        """
        stmt = select(AppAgentRoute).where(AppAgentRoute.agent_id == agent_id)
        if not current_user.is_superuser:
            stmt = stmt.where(AppAgentRoute.created_by == current_user.id)
        routes = db_session.exec(stmt).all()
        return [_route_to_public(db_session, r) for r in routes]

    @staticmethod
    def get_route(
        db_session: DBSession,
        route_id: uuid.UUID,
    ) -> AppAgentRoutePublic | None:
        route = db_session.get(AppAgentRoute, route_id)
        if not route:
            return None
        return _route_to_public(db_session, route)

    @staticmethod
    def get_route_for_agent(
        db_session: DBSession,
        agent_id: uuid.UUID,
        route_id: uuid.UUID,
        current_user: User,
    ) -> AppAgentRoutePublic | None:
        """Get a route, validating it belongs to agent_id and user has access.

        Returns None if not found or access denied (treat as not found for security).
        """
        route = db_session.get(AppAgentRoute, route_id)
        if not route or route.agent_id != agent_id:
            return None
        if not current_user.is_superuser and route.created_by != current_user.id:
            return None
        return _route_to_public(db_session, route)

    @staticmethod
    def update_route(
        db_session: DBSession,
        route_id: uuid.UUID,
        data: AppAgentRouteUpdate,
    ) -> AppAgentRoutePublic | None:
        """Update a route (superuser-only path — no ownership check)."""
        route = db_session.get(AppAgentRoute, route_id)
        if not route:
            return None
        update_dict = data.model_dump(exclude_unset=True)
        route.sqlmodel_update(update_dict)
        route.updated_at = datetime.now(UTC)
        db_session.add(route)
        db_session.commit()
        db_session.refresh(route)
        return _route_to_public(db_session, route)

    @staticmethod
    def update_route_for_agent(
        db_session: DBSession,
        agent_id: uuid.UUID,
        route_id: uuid.UUID,
        data: AppAgentRouteUpdate,
        current_user: User,
    ) -> AppAgentRoutePublic | None:
        """Update a route with ownership/permission checks.

        Raises ValueError for permission violations.
        Returns None if not found.

        Any user-driven edit via this public path flips
        ``is_auto_managed=False`` so subsequent bundle apply-updates leave
        the user's customisations alone.
        """
        route = db_session.get(AppAgentRoute, route_id)
        if not route or route.agent_id != agent_id:
            return None
        if not current_user.is_superuser and route.created_by != current_user.id:
            raise ValueError("Access denied")
        # Only superusers may set auto_enable_for_users=True
        if data.auto_enable_for_users and not current_user.is_superuser:
            raise ValueError("Only administrators can auto-enable routes for users")
        update_dict = data.model_dump(exclude_unset=True)
        update_dict.pop("agent_id", None)  # agent_id cannot be changed via update
        route.sqlmodel_update(update_dict)
        # Public PUT = user-driven edit. Flip the auto-managed flag so the
        # apply-update path stops overwriting this route.
        if route.is_auto_managed:
            route.is_auto_managed = False
        route.updated_at = datetime.now(UTC)
        db_session.add(route)
        db_session.commit()
        db_session.refresh(route)
        return _route_to_public(db_session, route)

    @staticmethod
    def delete_route(
        db_session: DBSession,
        route_id: uuid.UUID,
    ) -> bool:
        """Delete a route (superuser-only path — no ownership check)."""
        route = db_session.get(AppAgentRoute, route_id)
        if not route:
            return False
        db_session.delete(route)
        db_session.commit()
        return True

    @staticmethod
    def delete_route_for_agent(
        db_session: DBSession,
        agent_id: uuid.UUID,
        route_id: uuid.UUID,
        current_user: User,
    ) -> bool:
        """Delete a route with ownership/permission checks.

        Raises ValueError for permission violations.
        Returns False if not found.
        """
        route = db_session.get(AppAgentRoute, route_id)
        if not route or route.agent_id != agent_id:
            return False
        if not current_user.is_superuser and route.created_by != current_user.id:
            raise ValueError("Access denied")
        db_session.delete(route)
        db_session.commit()
        return True

    @staticmethod
    def assign_users(
        db_session: DBSession,
        route_id: uuid.UUID,
        user_ids: list[uuid.UUID],
        auto_enable: bool = False,
    ) -> list[AppAgentRouteAssignmentPublic]:
        """Add user assignments to a route (skip duplicates).

        auto_enable controls the initial is_enabled value for new assignments.
        """
        route = db_session.get(AppAgentRoute, route_id)
        if not route:
            return []

        # Fetch existing assignments for this route
        existing_stmt = select(AppAgentRouteAssignment).where(
            AppAgentRouteAssignment.route_id == route_id
        )
        existing = {a.user_id for a in db_session.exec(existing_stmt).all()}

        for user_id in user_ids:
            if user_id not in existing:
                a = AppAgentRouteAssignment(
                    route_id=route_id,
                    user_id=user_id,
                    is_enabled=auto_enable,
                )
                db_session.add(a)

        db_session.commit()

        # Return all current assignments
        all_assignments = db_session.exec(existing_stmt).all()
        return [
            AppAgentRouteAssignmentPublic(
                id=a.id,
                route_id=a.route_id,
                user_id=a.user_id,
                is_enabled=a.is_enabled,
                created_at=a.created_at,
            )
            for a in all_assignments
        ]

    @staticmethod
    def remove_assignment(
        db_session: DBSession,
        route_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        stmt = select(AppAgentRouteAssignment).where(
            AppAgentRouteAssignment.route_id == route_id,
            AppAgentRouteAssignment.user_id == user_id,
        )
        assignment = db_session.exec(stmt).first()
        if not assignment:
            return False
        db_session.delete(assignment)
        db_session.commit()
        return True

    @staticmethod
    def get_effective_routes_for_user(
        db_session: DBSession,
        user_id: uuid.UUID,
        channel: str = "app_mcp",
    ) -> list[EffectiveRoute]:
        """Get all active routes for a user (assigned + personal), filtered by channel.

        Returns unified EffectiveRoute objects. Only includes routes where:
        - Assigned route: is_active=True AND assignment is_enabled=True AND channel enabled
        - Personal route (UserAppAgentRoute): is_active=True AND channel enabled
        """
        results: list[EffectiveRoute] = []

        logger.info(
            "[EffectiveRoutes] Building routes for user=%s channel=%s",
            user_id, channel,
        )

        # Assigned routes (AppAgentRoute via AppAgentRouteAssignment)
        stmt = (
            select(AppAgentRoute, AppAgentRouteAssignment)
            .join(
                AppAgentRouteAssignment,
                AppAgentRouteAssignment.route_id == AppAgentRoute.id,
            )
            .where(
                AppAgentRouteAssignment.user_id == user_id,
                AppAgentRouteAssignment.is_enabled == True,  # noqa: E712
                AppAgentRoute.is_active == True,  # noqa: E712
            )
        )
        if channel == "app_mcp":
            stmt = stmt.where(AppAgentRoute.channel_app_mcp == True)  # noqa: E712

        assigned_rows = db_session.exec(stmt).all()
        logger.info("[EffectiveRoutes]   Assigned routes: %d", len(assigned_rows))
        for route, _assignment in assigned_rows:
            agent_name = _get_agent_name(db_session, route.agent_id)
            logger.info(
                "[EffectiveRoutes]     - %s (agent=%s, route=%s)",
                agent_name, route.agent_id, route.id,
            )
            results.append(
                EffectiveRoute(
                    route_id=route.id,
                    agent_id=route.agent_id,
                    agent_name=agent_name,
                    session_mode=route.session_mode,
                    trigger_prompt=route.trigger_prompt,
                    message_patterns=route.message_patterns,
                    source="admin",
                    prompt_examples=route.prompt_examples,
                )
            )

        # Personal routes (soft-deprecated UserAppAgentRoute table)
        user_stmt = select(UserAppAgentRoute).where(
            UserAppAgentRoute.user_id == user_id,
            UserAppAgentRoute.is_active == True,  # noqa: E712
        )
        if channel == "app_mcp":
            user_stmt = user_stmt.where(
                UserAppAgentRoute.channel_app_mcp == True  # noqa: E712
            )

        personal_rows = db_session.exec(user_stmt).all()
        logger.info("[EffectiveRoutes]   Personal routes: %d", len(personal_rows))
        for user_route in personal_rows:
            agent_name = _get_agent_name(db_session, user_route.agent_id)
            results.append(
                EffectiveRoute(
                    route_id=user_route.id,
                    agent_id=user_route.agent_id,
                    agent_name=agent_name,
                    session_mode=user_route.session_mode,
                    trigger_prompt=user_route.trigger_prompt,
                    message_patterns=user_route.message_patterns,
                    source="user",
                )
            )

        # Identity contacts — distinct owners who have active+enabled assignments for this user
        # One EffectiveRoute per person (not per binding). Stage 2 handles agent selection.
        from app.models.identity.identity_models import (
            IdentityAgentBinding,
            IdentityBindingAssignment,
        )

        # Debug: count raw identity assignments for this user (before filters)
        raw_assignments_stmt = select(IdentityBindingAssignment).where(
            IdentityBindingAssignment.target_user_id == user_id,
        )
        raw_assignments = db_session.exec(raw_assignments_stmt).all()
        logger.info(
            "[EffectiveRoutes]   Identity assignments (raw, all states): %d",
            len(raw_assignments),
        )
        for ra in raw_assignments:
            binding = db_session.get(IdentityAgentBinding, ra.binding_id)
            logger.info(
                "[EffectiveRoutes]     - assignment=%s binding=%s "
                "assign.is_active=%s assign.is_enabled=%s "
                "binding.is_active=%s binding.owner=%s",
                ra.id, ra.binding_id,
                ra.is_active, ra.is_enabled,
                binding.is_active if binding else "MISSING",
                binding.owner_id if binding else "MISSING",
            )

        identity_stmt = (
            select(User)
            .join(
                IdentityAgentBinding,
                IdentityAgentBinding.owner_id == User.id,
            )
            .join(
                IdentityBindingAssignment,
                IdentityBindingAssignment.binding_id == IdentityAgentBinding.id,
            )
            .where(
                IdentityBindingAssignment.target_user_id == user_id,
                IdentityBindingAssignment.is_active == True,  # noqa: E712
                IdentityBindingAssignment.is_enabled == True,  # noqa: E712
                IdentityAgentBinding.is_active == True,  # noqa: E712
            )
            .distinct()
        )

        identity_owners = db_session.exec(identity_stmt).all()
        logger.info("[EffectiveRoutes]   Identity contacts (filtered): %d", len(identity_owners))
        for owner in identity_owners:
            logger.info(
                "[EffectiveRoutes]     - owner=%s (%s)",
                owner.full_name or owner.email, owner.id,
            )
            identity_examples = AppAgentRouteService._build_identity_prompt_examples(
                db_session=db_session,
                owner_id=owner.id,
                owner_name=owner.full_name or "",
                owner_email=owner.email or "",
                target_user_id=user_id,
            )
            results.append(
                EffectiveRoute(
                    route_id=uuid.UUID(int=0),  # placeholder — identity uses owner_id
                    agent_id=uuid.UUID(int=0),  # placeholder — resolved in Stage 2
                    agent_name=owner.full_name or owner.email or "",
                    session_mode="conversation",  # Stage 2 overrides with binding's session_mode
                    trigger_prompt=(
                        f"Contact {owner.full_name or owner.email} ({owner.email}). "
                        f"Routes to their available agents."
                    ),
                    message_patterns=None,
                    source="identity",
                    identity_owner_id=owner.id,
                    identity_owner_name=owner.full_name or "",
                    identity_owner_email=owner.email or "",
                    prompt_examples=identity_examples,
                )
            )

        logger.info("[EffectiveRoutes] Total effective routes: %d", len(results))
        return results

    @staticmethod
    def sync_router_trigger_prompt_from_agent(
        db_session: DBSession,
        agent: Agent,
    ) -> AppAgentRoute | None:
        """Propagate ``agent.router_trigger_prompt`` to its auto-managed route.

        Idempotent: skips silently when no auto-managed route exists for
        this agent (an install where the auto-route creation was skipped
        because the agent had no trigger prompt at install time). Updates
        ``trigger_prompt``, ``name``, and ``updated_at`` on the route.

        Does not touch user-edited routes (``is_auto_managed=False``) —
        those are owned by the user.

        Returns the updated route or ``None`` if nothing was synced.
        """
        stmt = select(AppAgentRoute).where(
            AppAgentRoute.agent_id == agent.id,
            AppAgentRoute.is_auto_managed == True,  # noqa: E712
        )
        route = db_session.exec(stmt).first()
        if route is None:
            return None

        new_prompt = (agent.router_trigger_prompt or "").strip()
        if not new_prompt:
            # The agent's trigger prompt was cleared. Leave the route's
            # current value alone rather than writing an empty string into
            # a NOT NULL column. The route is still usable; the user can
            # delete it explicitly via the UI.
            return None

        changed = False
        if route.trigger_prompt != new_prompt:
            route.trigger_prompt = new_prompt
            changed = True
        if route.name != agent.name:
            route.name = agent.name
            changed = True
        if changed:
            route.updated_at = datetime.now(UTC)
            db_session.add(route)
            db_session.commit()
            db_session.refresh(route)
        return route

    # ── Conflict detection ───────────────────────────────────────────────
    #
    # Cheap install-time check: compare the auto-managed route's
    # ``trigger_prompt`` against the installer's other effective routes
    # using lowercased token-overlap (Jaccard) similarity. Surfaces
    # near-duplicate intents (e.g. "Calendar Planner" vs "Vacation
    # Planner") as a non-blocking toast on the install completion page.
    #
    # Stopwords aren't worth pruning — Jaccard already discounts common
    # tokens that appear in both sides. Threshold tuned conservatively
    # (≥ 0.45) so unrelated agents don't trigger noise.

    SIMILARITY_THRESHOLD: float = 0.45

    @staticmethod
    def _tokens_for_similarity(text: str | None) -> set[str]:
        """Tokenise ``text`` for Jaccard comparison.

        Lowercases, splits on non-alphanumerics, drops tokens shorter than
        3 chars (filters single letters / digits / very common short
        words). Returns a set of tokens.
        """
        if not text:
            return set()
        import re

        tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
        return {t for t in tokens if len(t) >= 3}

    @staticmethod
    def _jaccard_similarity(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        intersection = a & b
        union = a | b
        if not union:
            return 0.0
        return len(intersection) / len(union)

    @staticmethod
    def find_route_conflicts_for_agent(
        db_session: DBSession,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        threshold: float | None = None,
    ) -> RouteConflictResponse:
        """Find effective routes similar to this agent's auto-managed route.

        Args:
            db_session: Active DB session.
            agent_id: The freshly-installed agent to compare *against*.
            user_id: The installer — we scope effective routes to this
                user only (their assigned + personal routes on the
                App MCP channel).
            threshold: Override the default Jaccard threshold. Routes
                scoring below the threshold are excluded.

        Returns:
            :class:`RouteConflictResponse` with matches sorted by
            descending similarity. Self-route, identity routes, and
            routes that target the same ``agent_id`` are excluded.
        """
        cutoff = (
            threshold if threshold is not None
            else AppAgentRouteService.SIMILARITY_THRESHOLD
        )

        # Locate the agent's auto-managed route. If none exists (install
        # had no trigger prompt, or the route was deleted), there is
        # nothing to compare and we return an empty match list.
        # Filter on ``is_auto_managed=True`` so a sibling manual route
        # on the same agent (e.g. a developer added an extra route via
        # the UI) doesn't shadow the lookup — the docstring promises
        # "the agent's auto-managed route", not "some route".
        own_route_stmt = select(AppAgentRoute).where(
            AppAgentRoute.agent_id == agent_id,
            AppAgentRoute.created_by == user_id,
            AppAgentRoute.is_auto_managed == True,  # noqa: E712
        )
        own_route = db_session.exec(own_route_stmt).first()
        if own_route is None or not own_route.trigger_prompt:
            return RouteConflictResponse(matches=[])

        own_tokens = AppAgentRouteService._tokens_for_similarity(
            own_route.trigger_prompt
        )
        if not own_tokens:
            return RouteConflictResponse(matches=[])

        effective = AppAgentRouteService.get_effective_routes_for_user(
            db_session=db_session,
            user_id=user_id,
            channel="app_mcp",
        )

        matches: list[RouteConflictMatch] = []
        for r in effective:
            # Identity routes have placeholder ids and target multiple
            # agents — they don't represent a single comparable trigger
            # prompt, so skip them.
            if r.source == "identity":
                continue
            # Skip the route that belongs to this same agent (it's our
            # baseline, not a conflict).
            if r.agent_id == agent_id:
                continue
            other_tokens = AppAgentRouteService._tokens_for_similarity(
                r.trigger_prompt
            )
            score = AppAgentRouteService._jaccard_similarity(own_tokens, other_tokens)
            if score >= cutoff:
                matches.append(
                    RouteConflictMatch(
                        route_id=r.route_id,
                        agent_id=r.agent_id,
                        agent_name=r.agent_name,
                        trigger_prompt=r.trigger_prompt,
                        similarity=round(score, 3),
                    )
                )

        matches.sort(key=lambda m: m.similarity, reverse=True)
        return RouteConflictResponse(matches=matches)

    @staticmethod
    def toggle_admin_assignment(
        db_session: DBSession,
        assignment_id: uuid.UUID,
        user_id: uuid.UUID,
        is_enabled: bool,
    ) -> AppAgentRouteAssignmentPublic | None:
        """Allow a user to toggle their own route assignment on/off."""
        assignment = db_session.get(AppAgentRouteAssignment, assignment_id)
        if not assignment or assignment.user_id != user_id:
            return None
        assignment.is_enabled = is_enabled
        db_session.add(assignment)
        db_session.commit()
        db_session.refresh(assignment)
        return AppAgentRouteAssignmentPublic(
            id=assignment.id,
            route_id=assignment.route_id,
            user_id=assignment.user_id,
            is_enabled=assignment.is_enabled,
            created_at=assignment.created_at,
        )

    @staticmethod
    def _build_identity_prompt_examples(
        db_session: DBSession,
        owner_id: uuid.UUID,
        owner_name: str,
        owner_email: str,
        target_user_id: uuid.UUID,
    ) -> str | None:
        """Build prefixed prompt examples for an identity route.

        Aggregates prompt_examples from all active bindings accessible to the caller,
        prefixing each with the identity owner's name.
        """
        from app.models.identity.identity_models import (
            IdentityAgentBinding,
            IdentityBindingAssignment,
        )
        stmt = (
            select(IdentityAgentBinding)
            .join(
                IdentityBindingAssignment,
                IdentityBindingAssignment.binding_id == IdentityAgentBinding.id,
            )
            .where(
                IdentityAgentBinding.owner_id == owner_id,
                IdentityBindingAssignment.target_user_id == target_user_id,
                IdentityBindingAssignment.is_active == True,  # noqa: E712
                IdentityBindingAssignment.is_enabled == True,  # noqa: E712
                IdentityAgentBinding.is_active == True,  # noqa: E712
            )
        )
        lines: list[str] = []
        for binding in db_session.exec(stmt).all():
            if binding.prompt_examples:
                for raw_line in binding.prompt_examples.splitlines():
                    line = raw_line.strip()
                    if line:
                        lines.append(f"ask {owner_name} ({owner_email}) to {line}")
        return "\n".join(lines) if lines else None


# ---------------------------------------------------------------------------
# UserAppAgentRouteService
# ---------------------------------------------------------------------------


class UserAppAgentRouteService:
    """Business logic for user-created personal Application Agent Routes (soft-deprecated)."""

    @staticmethod
    def create_route(
        db_session: DBSession,
        user_id: uuid.UUID,
        data: UserAppAgentRouteCreate,
    ) -> UserAppAgentRoutePublic:
        """Create a personal route. Validates the agent belongs to the user."""
        agent = db_session.get(Agent, data.agent_id)
        if not agent or agent.owner_id != user_id:
            raise ValueError("Agent not found or not owned by user")

        route = UserAppAgentRoute(
            user_id=user_id,
            agent_id=data.agent_id,
            session_mode=data.session_mode,
            trigger_prompt=data.trigger_prompt,
            message_patterns=data.message_patterns,
            channel_app_mcp=data.channel_app_mcp,
            is_active=data.is_active,
        )
        db_session.add(route)
        try:
            db_session.commit()
        except IntegrityError:
            db_session.rollback()
            raise ValueError("A personal route for this agent already exists")
        db_session.refresh(route)
        return _user_route_to_public(db_session, route)

    @staticmethod
    def list_routes(
        db_session: DBSession,
        user_id: uuid.UUID,
    ) -> list[UserAppAgentRoutePublic]:
        routes = db_session.exec(
            select(UserAppAgentRoute).where(UserAppAgentRoute.user_id == user_id)
        ).all()
        return [_user_route_to_public(db_session, r) for r in routes]

    @staticmethod
    def update_route(
        db_session: DBSession,
        route_id: uuid.UUID,
        user_id: uuid.UUID,
        data: UserAppAgentRouteUpdate,
    ) -> UserAppAgentRoutePublic | None:
        route = db_session.get(UserAppAgentRoute, route_id)
        if not route or route.user_id != user_id:
            return None
        update_dict = data.model_dump(exclude_unset=True)
        route.sqlmodel_update(update_dict)
        route.updated_at = datetime.now(UTC)
        db_session.add(route)
        db_session.commit()
        db_session.refresh(route)
        return _user_route_to_public(db_session, route)

    @staticmethod
    def delete_route(
        db_session: DBSession,
        route_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        route = db_session.get(UserAppAgentRoute, route_id)
        if not route or route.user_id != user_id:
            return False
        db_session.delete(route)
        db_session.commit()
        return True

    @staticmethod
    def get_shared_routes(
        db_session: DBSession,
        user_id: uuid.UUID,
    ) -> list[SharedRoutePublic]:
        """Get all routes shared with a user via AppAgentRouteAssignment.

        Includes agent owner info and route creator (sharer) info for display.
        """
        stmt = (
            select(AppAgentRoute, AppAgentRouteAssignment)
            .join(
                AppAgentRouteAssignment,
                AppAgentRouteAssignment.route_id == AppAgentRoute.id,
            )
            .where(AppAgentRouteAssignment.user_id == user_id)
        )
        result = []
        for route, assignment in db_session.exec(stmt).all():
            agent = db_session.get(Agent, route.agent_id)
            agent_name = agent.name if agent else ""

            agent_owner_name = ""
            agent_owner_email = ""
            if agent:
                owner = db_session.get(User, agent.owner_id)
                if owner:
                    agent_owner_name = owner.full_name or ""
                    agent_owner_email = owner.email or ""

            creator = db_session.get(User, route.created_by)
            shared_by_name = ""
            if creator:
                shared_by_name = creator.full_name or creator.email or ""

            result.append(
                SharedRoutePublic(
                    route_id=route.id,
                    name=route.name,
                    agent_name=agent_name,
                    agent_owner_name=agent_owner_name,
                    agent_owner_email=agent_owner_email,
                    shared_by_name=shared_by_name,
                    session_mode=route.session_mode,
                    trigger_prompt=route.trigger_prompt,
                    message_patterns=route.message_patterns,
                    prompt_examples=route.prompt_examples,
                    is_active=route.is_active,
                    assignment_id=assignment.id,
                    is_enabled=assignment.is_enabled,
                )
            )
        return result

    @staticmethod
    def get_effective_route_or_raise(
        db_session: DBSession,
        user_id: uuid.UUID,
        route_id: uuid.UUID,
        *,
        exclude_identity: bool = True,
        channel: str = "app_mcp",
    ) -> EffectiveRoute:
        """Return the EffectiveRoute for ``route_id`` or raise ``ValueError``.

        Convenience wrapper around :meth:`get_effective_routes_for_user` for the
        common "find one specific route" case.

        Args:
            db_session: Active database session.
            user_id: The user whose effective routes are checked.
            route_id: The specific route UUID to look for.
            exclude_identity: When ``True`` (default), identity-source routes are
                excluded — used by the external surface where identity contacts have
                their own target type.
            channel: Passed through to :meth:`get_effective_routes_for_user`.

        Returns:
            The matching :class:`EffectiveRoute`.

        Raises:
            ValueError: Route not effective for this user or not found.
        """
        routes = AppAgentRouteService.get_effective_routes_for_user(
            db_session=db_session,
            user_id=user_id,
            channel=channel,
        )
        for r in routes:
            if r.route_id == route_id:
                if exclude_identity and r.source == "identity":
                    continue
                return r
        raise ValueError("Route not found or access denied")
