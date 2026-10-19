"""Install Readiness Gate (Phase 4 of install-experience-redesign).

The gate is a stateless function over an install's credential link state.
It is invoked at every user→agent dispatch boundary (chat, MCP, A2A,
webhook) BEFORE any LLM call.

Returns a :class:`GateResult` describing one of three states:

* ``"ready"`` — the install has every credential it needs; let the
  request through.
* ``"needs_setup"`` — at least one user-provided credential is still a
  placeholder. The setup page collects them.
* ``"publisher_broken"`` — at least one publisher-provided credential is
  missing / unshared. The publisher must fix this; the installer can
  optionally provide their own override credential via the setup page.

**Catalog skill credentials never block (D1).** A credential linked because a
catalog skill on the agent requires it — and not also claimed by the agent's
bundle revision — is dropped from the missing list. A skill is one capability
among many on an agent, unlike a bundle whose specs are its contract, so an
unfilled skill slot is a warning on the skill's Addons row, in the install
response and in the container SDK, not a refusal on every channel. Bundle
verdicts are unchanged (I10). See ``_drop_skill_provisioned``.

A publisher credential that *cannot* be shared is deliberately none of these.
The install links the installer's own AI credential in its place and runs, so
nothing is missing and the gate stays out of the way — see the note at the
unshareable branch in ``_scan_publisher_ai``.

The gate is purely a read-side helper. It DOES NOT mutate state, write
events, or persist anything. Callers are responsible for: persisting a
synthesised system-message reply, emitting WS events, and short-circuiting
their channel-specific message dispatch.

See ``docs/drafts/install-experience-redesign_plan.md`` §6 (gate shape)
and §9 (security) for the full spec.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.bundles.catalog import GateMissingReason
from app.models.credentials.ai_credential import AICredential
from app.models.credentials.credential import Credential
from app.models.credentials.link_models import AgentCredentialLink
from app.services.credentials.ai_credentials_service import ai_credentials_service
from app.services.credentials.credential_share_service import CredentialShareService

logger = logging.getLogger(__name__)


GateStatus = Literal["ready", "needs_setup", "publisher_broken"]


@dataclass
class GateMissingItem:
    """One credential that is blocking the install from being ready."""

    spec_name: str
    spec_type: str
    reason: GateMissingReason
    is_ai: bool = False
    #: The linked credential behind a service-credential item. Internal only:
    #: the dispatcher payload and the setup-status response list their keys
    #: explicitly and never serialise it.
    credential_id: uuid.UUID | None = None


@dataclass
class GateResult:
    """Outcome of a single gate check."""

    status: GateStatus
    missing: list[GateMissingItem] = field(default_factory=list)
    setup_url: str | None = None
    user_message: str = ""


class InstallReadinessGate:
    """Static-method service. See module docstring for semantics."""

    # ── Public API ────────────────────────────────────────────────

    @staticmethod
    def check(session: Session, install: Agent) -> GateResult:
        """Inspect ``install``'s credential state; return a typed verdict.

        Cheap (one indexed join + a couple of point-lookups). Safe to
        call on every inbound message — see plan §6.3 (gate caching).
        """
        missing = InstallReadinessGate.missing_for(session, install)
        if not missing:
            return GateResult(status="ready", missing=[], setup_url=None, user_message="")

        # Publisher-broken trumps needs-setup: if anything is broken on
        # the publisher side the user can't fix it from the setup page,
        # so we want the chat-level message to call that out specifically.
        is_publisher_broken = any(
            m.reason in (
                "publisher_credential_missing",
                "publisher_credential_unshared",
            )
            for m in missing
        )
        status: GateStatus = "publisher_broken" if is_publisher_broken else "needs_setup"

        setup_url = InstallReadinessGate._build_setup_url(install.id)
        user_message = InstallReadinessGate._format_user_message(missing, setup_url, status)
        return GateResult(
            status=status,
            missing=missing,
            setup_url=setup_url,
            user_message=user_message,
        )

    @staticmethod
    def missing_for(session: Session, install: Agent) -> list[GateMissingItem]:
        """Return the list of blocking credential items.

        Public so the install detail page (``GET /setup-status``) can
        present the same shape without re-deriving the markdown copy.
        """
        missing: list[GateMissingItem] = []
        missing.extend(InstallReadinessGate._scan_service_credentials(session, install))
        missing.extend(InstallReadinessGate._scan_unlinked_publisher_specs(session, install))
        missing.extend(InstallReadinessGate._scan_ai_credentials(session, install))
        return missing

    # ── Service-credential scanner ────────────────────────────────

    @staticmethod
    def _scan_service_credentials(
        session: Session, install: Agent
    ) -> list[GateMissingItem]:
        """Walk ``AgentCredentialLink`` rows for the install.

        For each link:
          * placeholder owned by installer → ``placeholder_empty``
          * foreign-owned, no longer shareable / no share row →
            ``publisher_credential_unshared``
          * link points at a credential UUID that no longer exists →
            ``publisher_credential_missing``
        """
        spec_lookup = InstallReadinessGate._spec_lookup_for_install(session, install)

        link_rows = session.exec(
            select(AgentCredentialLink).where(AgentCredentialLink.agent_id == install.id)
        ).all()

        items: list[GateMissingItem] = []
        for link in link_rows:
            cred = session.get(Credential, link.credential_id)
            if cred is None:
                # Best-effort spec resolution by credential id is impossible
                # once the row is gone; fall back to a generic label so the
                # frontend still has something to render.
                spec_name, spec_type = InstallReadinessGate._spec_for_credential(
                    spec_lookup, credential_id=link.credential_id, fallback_name="(missing)"
                )
                items.append(GateMissingItem(
                    spec_name=spec_name,
                    spec_type=spec_type,
                    reason="publisher_credential_missing",
                    is_ai=False,
                    credential_id=link.credential_id,
                ))
                continue

            spec_name, spec_type = InstallReadinessGate._spec_for_credential(
                spec_lookup,
                credential_id=cred.id,
                fallback_name=cred.name,
                fallback_type=cred.type.value if hasattr(cred.type, "value") else str(cred.type),
            )

            if cred.owner_id == install.owner_id:
                # User-provided spec (or owner == installer is the publisher
                # install). Placeholder ⇒ needs_setup.
                if cred.is_placeholder:
                    items.append(GateMissingItem(
                        spec_name=spec_name,
                        spec_type=spec_type,
                        reason="placeholder_empty",
                        is_ai=False,
                        credential_id=link.credential_id,
                    ))
                continue

            # Foreign-owned: must be shareable AND must have an active
            # share to the installer. (The installer is never the owner
            # at this branch — already guarded above.)
            if not cred.allow_sharing:
                items.append(GateMissingItem(
                    spec_name=spec_name,
                    spec_type=spec_type,
                    reason="publisher_credential_unshared",
                    is_ai=False,
                    credential_id=link.credential_id,
                ))
                continue

            # Direct or user-group share.
            if not CredentialShareService.has_share(
                session, cred.id, install.owner_id
            ):
                items.append(GateMissingItem(
                    spec_name=spec_name,
                    spec_type=spec_type,
                    reason="publisher_credential_unshared",
                    is_ai=False,
                    credential_id=link.credential_id,
                ))
        if items:
            items = InstallReadinessGate._drop_skill_provisioned(
                session, install, items
            )
        return items

    @staticmethod
    def _scan_unlinked_publisher_specs(
        session: Session, install: Agent
    ) -> list[GateMissingItem]:
        """Bundle specs whose publisher credential is no longer even linked.

        The link scan above sees only what the agent still carries, which was
        enough while a revoked share left its link behind. Revocation now
        deletes the recipient's links (so the value leaves their containers),
        and without this pass the bundle's own contract would go quiet exactly
        when it breaks — the installer would see a ``ready`` agent that cannot
        do the thing it was installed for (I10).

        Only ``publisher_credential_id`` specs are scanned, and only when
        nothing links that credential any more:

          * the credential row is gone → ``publisher_credential_missing``
          * it exists but the installer can no longer reach it (sharing off, or
            no share row) → ``publisher_credential_unshared``
          * it exists and is still reachable → silent. The installer unlinked a
            credential they can re-link themselves, which never blocked the
            agent before either.
        """
        from app.models.bundles.agent_bundle_revision import AgentBundleRevision

        if install.installed_revision_id is None:
            return []
        revision = session.get(AgentBundleRevision, install.installed_revision_id)
        if revision is None:
            return []
        specs = [
            spec
            for spec in revision.required_credential_specs or []
            if isinstance(spec, dict) and spec.get("publisher_credential_id")
        ]
        if not specs:
            return []

        linked_ids = {
            link.credential_id
            for link in session.exec(
                select(AgentCredentialLink).where(
                    AgentCredentialLink.agent_id == install.id
                )
            ).all()
        }

        items: list[GateMissingItem] = []
        for spec in specs:
            try:
                credential_id = uuid.UUID(str(spec["publisher_credential_id"]))
            except (ValueError, TypeError):
                continue
            if credential_id in linked_ids:
                continue  # the link scan already judged it

            spec_name = spec.get("name") or "(missing)"
            spec_type = spec.get("type") or ""
            cred = session.get(Credential, credential_id)
            if cred is None:
                items.append(GateMissingItem(
                    spec_name=spec_name,
                    spec_type=spec_type,
                    reason="publisher_credential_missing",
                    is_ai=False,
                    credential_id=credential_id,
                ))
                continue
            if cred.owner_id == install.owner_id:
                continue
            if not cred.allow_sharing or not CredentialShareService.has_share(
                session, cred.id, install.owner_id
            ):
                items.append(GateMissingItem(
                    spec_name=spec_name,
                    spec_type=spec_type,
                    reason="publisher_credential_unshared",
                    is_ai=False,
                    credential_id=credential_id,
                ))
        return items

    @staticmethod
    def _drop_skill_provisioned(
        session: Session, install: Agent, items: list[GateMissingItem]
    ) -> list[GateMissingItem]:
        """D1: a catalog skill's credential never blocks the agent.

        Drops the items whose credential is linked because a catalog skill on
        the agent requires it, unless the agent's bundle revision claims that
        credential too — bundle verdicts are unchanged (I10). The skill's
        missing credential surfaces as a warning on its Addons row, in the
        install response and in the container SDK instead. Called only with a
        non-empty list, so a ready agent pays no extra query.
        """
        # Lazy: the skills domain sits above bundles in the import graph.
        from app.services.skills.skill_credential_requirements import (
            SkillSlotIndex,
        )

        skill_credential_ids = SkillSlotIndex.build_for_agent(
            session, install
        ).skill_provisioned_credential_ids()
        if not skill_credential_ids:
            return items
        return [
            item for item in items if item.credential_id not in skill_credential_ids
        ]

    # ── AI-credential scanner ─────────────────────────────────────

    @staticmethod
    def _scan_ai_credentials(
        session: Session, install: Agent
    ) -> list[GateMissingItem]:
        """Check the bundle-level publisher AI credential references.

        When the installer IS the publisher, no ``AICredentialShare`` is
        needed (publisher uses their own row directly); we still verify
        the row exists.
        """
        if install.bundle_uuid is None:
            return []

        bundle = session.get(AgentBundle, install.bundle_uuid)
        if bundle is None:
            return []

        items: list[GateMissingItem] = []
        for slot, ai_cred_id in (
            ("conversation", bundle.publisher_ai_credential_conversation_id),
            ("building", bundle.publisher_ai_credential_building_id),
        ):
            if ai_cred_id is None:
                continue

            ai_cred = session.get(AICredential, ai_cred_id)
            if ai_cred is None:
                items.append(GateMissingItem(
                    spec_name=f"AI ({slot})",
                    spec_type="ai_credential",
                    reason="publisher_credential_missing",
                    is_ai=True,
                ))
                continue

            # Skip share check when the installer owns the AI credential
            # (publisher install case).
            if ai_cred.owner_id == install.owner_id:
                continue

            # Direct or user-group share.
            if ai_credentials_service.has_share(
                session, ai_cred.id, install.owner_id
            ):
                # **An existing share is checked before the policy, and the
                # order is the point.** ``is_shareable`` answers "may a share be
                # *created* now". It does not answer "does one exist". Three
                # separate facts live here and the previous ordering collapsed
                # them into one false sentence:
                #
                #   1. the share exists;
                #   2. it still works — this installer can use the credential;
                #   3. it will not be created again, for this or any other
                #      installer, because the credential is admin-managed.
                #
                # Only (3) is about policy, and reporting it as
                # ``publisher_credential_unshareable`` told an installer whose
                # install was working that their publisher's credential "cannot
                # be shared". This list is "what the install is *missing*", and
                # a credential the installer can already use is not missing, so
                # the honest answer here is nothing at all. Fact (3) belongs to
                # the publisher, at publish time, where somebody can act on it —
                # see ``PublishService.publisher_ai_credential_notices``.
                #
                # **Accepted consequence, recorded as accepted.** Existing share
                # rows are left in place: revoking or migrating them would break
                # installs that work today, and that trade was taken
                # deliberately. It means a publisher who never opens the publish
                # flow again leaves such a share live indefinitely. That is the
                # cost of not breaking working installs, not an oversight to be
                # tidied up later.
                continue

            # No share, so the question is whether one could be made. Answered
            # by the same predicate the share path enforces rather than inferred
            # from the absence of a row: a credential the platform provisioned
            # for one person can never be shared, and telling the publisher to
            # share it is telling them to do something the server will refuse.
            if not ai_credentials_service.is_shareable(ai_cred):
                # **Not missing, so not reported.** This is the same conclusion
                # the existing-share branch above reaches, for the same reason,
                # and it holds here because of what happens at install time:
                # ``InstallService._linkable_publisher_ai_credential`` returns
                # ``None`` for exactly this credential, so the environment never
                # links it and resolves the installer's own AI credential
                # instead. The agent has a working key. The install is not
                # missing anything.
                #
                # It used to be reported, which made the status
                # ``publisher_broken`` and blocked every inbound message on an
                # install that ran fine. Worse, it was unclearable: the scan
                # reads ``bundle.publisher_ai_credential_*_id``, which no
                # installer action changes, so an installer could add and
                # default their own credential — the very credential the agent
                # was already using — and still be refused.
                #
                # Fact (3) from the note above is still true and still worth
                # someone knowing: this credential will never be shared, for
                # this or any other installer. It belongs to the publisher, at
                # publish time, where somebody can act on it —
                # ``PublishService.publisher_ai_credential_notices``. Dropping
                # it here loses no information; it stops telling the wrong
                # person a thing they cannot act on.
                #
                # The trade this accepts: an installer is silently using their
                # own model rather than the one the bundle was designed around.
                # That is the same position every ``provided_by="user"`` spec is
                # in, and it is the publisher's to disclose.
                continue

            items.append(GateMissingItem(
                spec_name=ai_cred.name or f"AI ({slot})",
                spec_type="ai_credential",
                reason="publisher_credential_unshared",
                is_ai=True,
            ))
        return items

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _spec_lookup_for_install(
        session: Session, install: Agent
    ) -> dict[str, dict]:
        """Build a ``credential_id → spec`` map from the installed revision.

        Used to enrich gate items with the original spec name and type
        instead of the placeholder credential's name. Falls back silently
        when the revision is missing or the JSON shape is older.
        """
        from app.models.bundles.agent_bundle_revision import AgentBundleRevision

        if install.installed_revision_id is None:
            return {}
        revision = session.get(AgentBundleRevision, install.installed_revision_id)
        if revision is None:
            return {}

        lookup: dict[str, dict] = {}
        for spec in revision.required_credential_specs or []:
            pub_id_raw = spec.get("publisher_credential_id")
            if pub_id_raw:
                lookup[str(pub_id_raw)] = spec
            spec_name = spec.get("name")
            if spec_name:
                lookup[f"name:{spec_name}"] = spec
        return lookup

    @staticmethod
    def _spec_for_credential(
        spec_lookup: dict[str, dict],
        *,
        credential_id: uuid.UUID,
        fallback_name: str,
        fallback_type: str = "",
    ) -> tuple[str, str]:
        """Resolve ``(spec_name, spec_type)`` for a credential id.

        Service credentials in the gate context don't carry a back-ref to
        the spec, so this is a best-effort enrichment. Falls back to the
        credential's own name when no spec entry matches.
        """
        spec = spec_lookup.get(str(credential_id))
        if spec:
            return spec.get("name") or fallback_name, spec.get("type") or fallback_type
        return fallback_name, fallback_type

    @staticmethod
    def _build_setup_url(install_id: uuid.UUID) -> str:
        """Setup URL — points at the agent detail page's Credentials tab.

        There is no dedicated setup page; users fix missing credentials
        one by one from the agent's own Credentials tab, where each
        placeholder/incomplete row is highlighted.
        """
        host = (settings.FRONTEND_HOST or "").rstrip("/")
        return f"{host}/agent/{install_id}#credentials"

    @staticmethod
    def _format_user_message(
        missing: list[GateMissingItem],
        setup_url: str | None,
        status: GateStatus,
    ) -> str:
        """Render the plain-text reply the gate emits as a system message.

        Chat / MCP / A2A all reuse this body. Channels that can render
        rich UI (Cinna chat) read ``install_setup_required`` metadata and
        render their own navigation; external clients receive the URL in
        the structured ``setup_url`` field instead of inside the message.
        """
        if status == "ready":
            return ""

        items_md = "\n".join(
            f"- {m.spec_name} ({m.spec_type})" for m in missing
        )

        if status == "publisher_broken":
            lead = (
                "This bundle's publisher-provided credentials are unavailable. "
                "The publisher needs to fix this, or you can supply your own "
                "credentials from the agent's Credentials tab."
            )
        else:
            lead = (
                "Setup needed before this agent can run. Open the agent's "
                "Credentials tab and fill in the missing values one by one."
            )

        return f"{lead}\n{items_md}"
