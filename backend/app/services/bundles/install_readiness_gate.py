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
from app.models.credentials.ai_credential_share import AICredentialShare
from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import CredentialShare
from app.models.credentials.link_models import AgentCredentialLink
from app.services.credentials.ai_credentials_service import ai_credentials_service

logger = logging.getLogger(__name__)


GateStatus = Literal["ready", "needs_setup", "publisher_broken"]


@dataclass
class GateMissingItem:
    """One credential that is blocking the install from being ready."""

    spec_name: str
    spec_type: str
    reason: GateMissingReason
    is_ai: bool = False


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
                "publisher_credential_unshareable",
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
                ))
                continue

            share = session.exec(
                select(CredentialShare).where(
                    CredentialShare.credential_id == cred.id,
                    CredentialShare.shared_with_user_id == install.owner_id,
                )
            ).first()
            if share is None:
                items.append(GateMissingItem(
                    spec_name=spec_name,
                    spec_type=spec_type,
                    reason="publisher_credential_unshared",
                    is_ai=False,
                ))
        return items

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

            share = session.exec(
                select(AICredentialShare).where(
                    AICredentialShare.ai_credential_id == ai_cred.id,
                    AICredentialShare.shared_with_user_id == install.owner_id,
                )
            ).first()
            if share is not None:
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
                items.append(GateMissingItem(
                    spec_name=ai_cred.name or f"AI ({slot})",
                    spec_type="ai_credential",
                    reason="publisher_credential_unshareable",
                    is_ai=True,
                ))
            else:
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

        if any(m.reason == "publisher_credential_unshareable" for m in missing):
            # Said separately because the generic publisher-broken copy tells the
            # publisher to fix it, and this one cannot be fixed by them: the
            # credential is provisioned per-user by policy. The installer's own
            # route out is the same, so the second sentence is the actionable
            # one.
            lead = (
                "This bundle is wired to an AI credential that was provisioned "
                "for one person and cannot be shared. The publisher needs to "
                "point the bundle at a shareable credential; in the meantime "
                "you can supply your own from the agent's Credentials tab."
            )
        elif status == "publisher_broken":
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
