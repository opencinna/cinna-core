"""Install-time credential provisioning: the one writer of install shares and links.

Bundle install and catalog skill install both turn a revision's
``required_credential_specs`` into rows for the installing agent — a
``CredentialShare`` from the publisher, ``AgentCredentialLink`` rows and
installer-owned placeholder ``Credential`` rows. They differ in policy, not in
mechanism, so one provisioner runs both and :class:`ProvisionPolicy` carries
every behavioural difference (D2).

* **Bundle policy** reproduces the historical ``InstallService`` loop exactly
  (I2): template → publisher → user, honouring install-form selections, and
  raising :class:`ProvisioningSelectionError` for a forbidden selection.
* **Skill policy** never fails an install for a per-slot reason (I8) and is
  idempotent (I9): a credential that already carries the slot is linked before
  any placeholder is created, and :meth:`CredentialProvisioner._ensure_link` is
  the single link-insert path.

The provisioner is synchronous and never commits; the caller owns the
transaction.
"""

import functools
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.security import encrypt_field
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.credentials.credential import Credential, CredentialType
from app.models.credentials.credential_share import (
    SHARE_SOURCE_SKILL_LOCAL_INSTALL,
    CredentialShare,
)
from app.models.credentials.link_models import AgentCredentialLink
from app.models.skills.schemas import SkillLocalSlotPublic, SkillSlotOutcome
from app.models.users.user import User
from app.services.bundles.credential_spec import (
    ParsedCredentialSpec,
    parse_credential_spec,
)
from app.services.credentials.credentials_service import CredentialsService

logger = logging.getLogger(__name__)


#: C4, defined once on the public schema.
SlotOutcome = SkillSlotOutcome


@dataclass(frozen=True)
class PublisherBoundary:
    """The package publisher a ``provided_by="publisher"`` credential must belong to.

    Passing ``None`` instead of a boundary skips the ownership check (a bundle
    install whose bundle row is gone). A boundary whose ``user_id`` is ``None``
    still runs the check and so rejects every publisher credential — the
    distinction a bare ``uuid.UUID | None`` cannot express (D2).
    """

    user_id: uuid.UUID | None


@dataclass(frozen=True)
class ProvisionPolicy:
    """Everything bundle and skill provisioning do differently (D2)."""

    share_source: Literal["bundle_install", "skill_install"]
    #: Link a credential that already carries the slot instead of creating
    #: one (D3). Also selects the skill decision tree over the bundle loop.
    auto_link_by_slot: bool
    #: ``"suffixed"`` → ``"<name> (placeholder)"``; ``"slot"`` → the slot.
    placeholder_name_mode: Literal["suffixed", "slot"]
    placeholder_notes: str
    #: Stamp ``service_uri=slot`` and the agent's workspace on created rows.
    placeholder_stamps_slot: bool
    template_notes_fallback: str
    honour_user_selections: bool


BUNDLE_INSTALL_POLICY = ProvisionPolicy(
    share_source="bundle_install",
    auto_link_by_slot=False,
    placeholder_name_mode="suffixed",
    placeholder_notes="Placeholder for required bundle credential.",
    placeholder_stamps_slot=False,
    template_notes_fallback="Created from bundle template.",
    honour_user_selections=True,
)

SKILL_INSTALL_POLICY = ProvisionPolicy(
    share_source="skill_install",
    auto_link_by_slot=True,
    placeholder_name_mode="slot",
    placeholder_notes=(
        "Required by an installed skill. Fill it in on the agent's Credentials tab."
    ),
    placeholder_stamps_slot=True,
    template_notes_fallback="Created from a skill template.",
    honour_user_selections=False,
)


@dataclass
class SlotProvision:
    spec_name: str
    spec_type: str
    slot: str | None
    provided_by: str
    description: str | None
    outcome: SlotOutcome
    credential_id: uuid.UUID | None
    #: Preview hides an unshared publisher id, but can expose configuration state.
    is_placeholder: bool | None = None


@dataclass
class ProvisionReport:
    items: list[SlotProvision] = field(default_factory=list)
    #: A publisher or template spec could not be honoured as published.
    degraded: bool = False
    #: Any share, link or credential row was created.
    changed: bool = False


class ProvisioningSelectionError(Exception):
    """An install-form selection the spec does not allow (bundle policy only)."""

    def __init__(self, spec_name: str, message: str) -> None:
        super().__init__(message)
        self.spec_name = spec_name
        self.message = message


@dataclass(frozen=True)
class _SlotDecision:
    """Where the skill decision tree lands for one spec, before any write."""

    outcome: SlotOutcome
    #: The credential to link for ``already_linked`` / ``linked_publisher`` /
    #: ``linked_existing``; ``None`` when a row still has to be created.
    credential: Credential | None
    publisher_failed: bool = False


def spec_slot(parsed: ParsedCredentialSpec) -> str:
    """The slot a parsed spec addresses (C2)."""
    return parsed.service_uri or parsed.name


def bundle_claimed_credential_ids(
    session: Session, *, agent: Agent, linked_credentials: Iterable[Credential]
) -> set[uuid.UUID]:
    """Ids among the agent's linked credentials that its bundle revision claims.

    ``linked_credentials`` must be **every** credential linked to ``agent``:
    rule (c) needs the whole set to tell an answered spec from an unaccounted
    one. Costs one query, and only when the agent has an installed bundle
    revision; agents without one claim nothing.

    A linked credential is claimed when any of:

    a. its id is a bundle spec's ``publisher_credential_id``;
    b. its name is a bundle spec's name, or ``"<spec name> (placeholder)"``;
    c. its type is the type of a bundle spec that no linked credential answers
       through (a) or (b). That spec's pick is unrecorded (an install-form
       ``use_existing`` selection, a later manual re-link), so it claims every
       linked credential of its type.

    Coordinator ruling (Phase 2): over-claiming only makes a skill credential
    on a bundle agent behave as before D1 (gated, placeholder kept on
    uninstall); under-claiming would drop a bundle blocker from the readiness
    gate (I10) or release a placeholder the bundle depends on. The gate's own
    item-name enrichment is separate and unchanged.
    """
    if agent.installed_revision_id is None:
        return set()
    revision = session.get(AgentBundleRevision, agent.installed_revision_id)
    if revision is None:
        return set()
    specs = [
        parsed
        for raw in revision.required_credential_specs or []
        if (parsed := parse_credential_spec(raw)) is not None
    ]
    if not specs:
        return set()

    credentials = list(linked_credentials)
    claimed: set[uuid.UUID] = set()
    unaccounted_types: set[str] = set()
    for parsed in specs:
        names = {parsed.name, f"{parsed.name} (placeholder)"}
        answering = [
            credential.id
            for credential in credentials
            if (
                parsed.publisher_credential_id is not None
                and credential.id == parsed.publisher_credential_id
            )
            or credential.name in names
        ]
        if answering:
            claimed.update(answering)
        else:
            unaccounted_types.add(parsed.type)
    claimed.update(
        credential.id
        for credential in credentials
        if credential_type_value(credential) in unaccounted_types
    )
    return claimed


def credential_type_value(credential: Credential) -> str:
    """A credential's type as its plain string value."""
    return str(getattr(credential.type, "value", credential.type))


class CredentialProvisioner:
    @staticmethod
    def provision(
        session: Session,
        *,
        agent: Agent,
        specs: list[ParsedCredentialSpec],
        publisher: PublisherBoundary | None,
        policy: ProvisionPolicy,
        user_selections: dict | None = None,
    ) -> ProvisionReport:
        """Share, link or create a credential for every spec on ``agent``.

        Flushes, never commits. Raises :class:`ProvisioningSelectionError`
        only under a policy that honours user selections (bundles); the skill
        policy logs per-slot problems and degrades instead (I8).
        """
        report = ProvisionReport()
        if policy.auto_link_by_slot:
            CredentialProvisioner._provision_by_slot(
                session,
                agent=agent,
                specs=specs,
                publisher=publisher,
                policy=policy,
                report=report,
            )
        else:
            CredentialProvisioner._provision_bundle_specs(
                session,
                agent=agent,
                specs=specs,
                publisher=publisher,
                policy=policy,
                user_selections=user_selections,
                report=report,
            )
        return report

    @staticmethod
    def provision_local(
        session: Session,
        *,
        user: User,
        specs: list[ParsedCredentialSpec],
        publisher: PublisherBoundary,
    ) -> list[SkillLocalSlotPublic]:
        """Share each publisher-provided slot with ``user`` for a Desktop install.

        No agent and no link: the only write is a ``CredentialShare`` stamped
        ``skill_local_install`` (first-writer-wins, with one exception: a
        ``skill_install`` share is promoted to ``skill_local_install``, both
        being "automatic"; a ``bundle_install`` share is left alone and the
        slot answers ``not_allowed_locally``), which is what puts the credential into ``user``'s
        ``/external/credentials``. Each ``publisher`` spec is re-checked live:
        the credential still exists, is owned by the package publisher, still
        allows sharing, still carries the spec's type and slot, works on a
        device, and its owner consented to local use. ``user`` and
        ``template`` specs are filled by the installer. Idempotent. Flushes,
        never commits. Role plays no part (a credential exchange is never
        gated on role).
        """
        from app.services.credentials.desktop_credential_service import (
            LOCALLY_COMPATIBLE_TYPES,
        )

        slots: list[SkillLocalSlotPublic] = []
        for parsed in specs:
            slot = spec_slot(parsed)
            outcome = functools.partial(CredentialProvisioner._local_slot, parsed)
            if parsed.provided_by != "publisher":
                slots.append(outcome("user_provides"))
                continue
            credential = (
                session.get(Credential, parsed.publisher_credential_id)
                if parsed.publisher_credential_id is not None
                else None
            )
            if (
                credential is None
                or publisher.user_id is None
                or credential.owner_id != publisher.user_id
                or not credential.allow_sharing
                or credential_type_value(credential) != parsed.type
                or credential.service_uri != slot
            ):
                slots.append(outcome("not_shareable"))
                continue
            if credential.owner_id == user.id:
                # The installer is the publisher: the credential is theirs.
                slots.append(outcome("already_shared", credential.id))
                continue
            if (
                not credential.allow_local_use
                or credential.type not in LOCALLY_COMPATIBLE_TYPES
                or credential.type == CredentialType.AGENT_API
            ):
                slots.append(outcome("not_allowed_locally"))
                continue
            existing = CredentialProvisioner._share_row(
                session, credential_id=credential.id, user_id=user.id
            )
            if existing is not None:
                if existing.source == "bundle_install":
                    # A bundle share stays the bundle's; Desktop never copies it.
                    slots.append(outcome("not_allowed_locally"))
                    continue
                if existing.source == "skill_install":
                    # The one exception to first-writer-wins: both sources are
                    # "automatic", and only the local one reaches a Desktop.
                    existing.source = SHARE_SOURCE_SKILL_LOCAL_INSTALL
                    session.add(existing)
                    session.flush()
                slots.append(outcome("already_shared", credential.id))
                continue
            inserted = CredentialProvisioner._insert_share(
                session,
                credential=credential,
                user_id=user.id,
                source=SHARE_SOURCE_SKILL_LOCAL_INSTALL,
            )
            slots.append(
                outcome("shared" if inserted else "already_shared", credential.id)
            )
        return slots

    @staticmethod
    def _local_slot(
        parsed: ParsedCredentialSpec,
        outcome: str,
        credential_id: uuid.UUID | None = None,
    ) -> SkillLocalSlotPublic:
        return SkillLocalSlotPublic(
            slot=spec_slot(parsed),
            type=parsed.type,
            provided_by=parsed.provided_by,
            outcome=outcome,
            credential_id=credential_id,
        )

    @staticmethod
    def _insert_share(
        session: Session, *, credential: Credential, user_id: uuid.UUID, source: str
    ) -> bool:
        """Insert a share in a savepoint; ``False`` when a concurrent request
        inserted the same ``(credential, user)`` row first."""
        savepoint = session.begin_nested()
        try:
            session.add(
                CredentialShare(
                    credential_id=credential.id,
                    shared_with_user_id=user_id,
                    shared_by_user_id=credential.owner_id,
                    access_level="read",
                    source=source,
                )
            )
            session.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            return False
        return True

    @staticmethod
    def preview(
        session: Session,
        *,
        agent: Agent,
        specs: list[ParsedCredentialSpec],
        publisher: PublisherBoundary | None,
        policy: ProvisionPolicy,
    ) -> list[SlotProvision]:
        """What :meth:`provision` would do for each spec, without writing.

        ``credential_id`` is set for ``already_linked`` / ``linked_existing``,
        and for ``linked_publisher`` only when the installer already owns the
        publisher credential or has a share on it.

        Raises:
            ValueError: for a policy without slot matching (bundles have no
                preview).
        """
        if not policy.auto_link_by_slot:
            raise ValueError(
                "CredentialProvisioner.preview supports slot-matching policies only."
            )

        linked = CredentialProvisioner._linked_credentials(session, agent.id)
        items: list[SlotProvision] = []
        for parsed in specs:
            cred_type = CredentialProvisioner._slot_credential_type(parsed, log=False)
            if cred_type is None:
                continue
            decision = CredentialProvisioner._decide_slot(
                session,
                agent=agent,
                parsed=parsed,
                cred_type=cred_type,
                publisher=publisher,
                linked=linked,
                log=False,
            )
            credential_id: uuid.UUID | None = None
            if decision.credential is not None:
                if decision.outcome != "linked_publisher" or (
                    CredentialProvisioner._installer_can_see(
                        session, agent=agent, credential=decision.credential
                    )
                ):
                    credential_id = decision.credential.id
                # Mirror provision: a credential it would link counts as
                # linked for the specs after it.
                if all(existing.id != decision.credential.id for existing in linked):
                    linked.append(decision.credential)
            items.append(
                CredentialProvisioner._slot_item(
                    parsed,
                    slot=spec_slot(parsed),
                    outcome=decision.outcome,
                    credential_id=credential_id,
                    is_placeholder=(
                        decision.credential.is_placeholder
                        if decision.credential is not None
                        else None
                    ),
                )
            )
        return items

    @staticmethod
    def release_skill_slots(
        session: Session,
        *,
        agent: Agent,
        released: list[ParsedCredentialSpec],
        retained: list[ParsedCredentialSpec],
    ) -> bool:
        """Unlink the skill placeholders of slots nothing else on the agent needs.

        Only installer-owned placeholders carrying a released (type, slot) that
        no retained spec also declares are considered, and bundle-claimed ones
        are skipped. A released placeholder that no agent links any more is
        deleted (D5). Real credentials and shared credentials are never
        touched. Flushes, never commits. Returns whether anything changed.
        """
        released_keys = CredentialProvisioner._slot_keys(
            released
        ) - CredentialProvisioner._slot_keys(retained)
        if not released_keys:
            return False

        linked = CredentialProvisioner._linked_credentials(session, agent.id)
        candidates = [
            credential
            for credential in linked
            if credential.is_placeholder
            and credential.owner_id == agent.owner_id
            and (credential.type, credential.service_uri) in released_keys
        ]
        if not candidates:
            return False

        # The bundle-claim rule needs every linked credential, not only the
        # candidates (an unaccounted bundle spec claims by type).
        claimed = bundle_claimed_credential_ids(
            session, agent=agent, linked_credentials=linked
        )
        changed = False
        for credential in candidates:
            if credential.id in claimed:
                continue
            link = session.get(
                AgentCredentialLink,
                {"agent_id": agent.id, "credential_id": credential.id},
            )
            if link is None:
                continue
            session.delete(link)
            session.flush()
            changed = True

            still_linked = session.exec(
                select(AgentCredentialLink).where(
                    AgentCredentialLink.credential_id == credential.id
                )
            ).first()
            if still_linked is None and credential.is_placeholder:
                session.delete(credential)
                session.flush()
        return changed

    # ── Bundle policy ────────────────────────────────────────────────────

    @staticmethod
    def _provision_bundle_specs(
        session: Session,
        *,
        agent: Agent,
        specs: list[ParsedCredentialSpec],
        publisher: PublisherBoundary | None,
        policy: ProvisionPolicy,
        user_selections: dict | None,
        report: ProvisionReport,
    ) -> None:
        """The historical bundle install loop, unchanged in behaviour (I2).

        Order per spec: template → publisher → user. A template or publisher
        spec that cannot be honoured marks the report degraded and falls
        through to the user branch, which links the installer's selection or
        creates a placeholder.
        """
        for parsed in specs:
            user_selection = (
                user_selections.get(parsed.name)
                if policy.honour_user_selections and user_selections
                else None
            )
            publisher_failed = False

            # ── Template-provided branch ─────────────────────────────────
            # The publisher chose to ship non-private fields as template
            # defaults; the installer only needs to fill in the private
            # ones. An explicit ``mode="use_existing"`` selection wins over
            # a half-filled template.
            if parsed.provided_by == "template":
                wants_existing = (
                    isinstance(user_selection, dict)
                    and user_selection.get("mode") == "use_existing"
                    and user_selection.get("credential_id")
                )
                if not wants_existing:
                    materialised = CredentialProvisioner._materialise_template(
                        session, agent=agent, parsed=parsed, policy=policy
                    )
                    if materialised is not None:
                        CredentialProvisioner._ensure_link(
                            session, agent.id, materialised.id
                        )
                        report.changed = True
                        report.items.append(
                            CredentialProvisioner._slot_item(
                                parsed,
                                slot=parsed.service_uri,
                                outcome="template_materialised",
                                credential_id=materialised.id,
                            )
                        )
                        continue
                    # Bad spec data falls through to a regular placeholder so
                    # the install still completes and the runtime gate guides
                    # the installer.
                    report.degraded = True
                    logger.warning(
                        "Failed to materialise template credential for spec '%s' "
                        "on install %s — falling back to placeholder",
                        parsed.name,
                        agent.id,
                    )

            # ── Publisher-provided branch ─────────────────────────────────
            if (
                parsed.provided_by == "publisher"
                and parsed.publisher_credential_id is not None
            ):
                # An explicit personal override of a publisher spec is not
                # permitted.
                if (
                    isinstance(user_selection, dict)
                    and user_selection.get("mode") == "use_existing"
                ):
                    raise ProvisioningSelectionError(
                        parsed.name,
                        (
                            f"Spec '{parsed.name}' is provided by the publisher and "
                            "cannot be overridden with a personal credential. "
                            "Re-submit with mode='publisher_provides' or omit "
                            "the entry."
                        ),
                    )
                publisher_cred = CredentialProvisioner._usable_publisher_credential(
                    session,
                    agent=agent,
                    credential_id=parsed.publisher_credential_id,
                    spec_name=parsed.name,
                    publisher=publisher,
                    log=True,
                )
                if publisher_cred is not None:
                    if CredentialProvisioner._share_and_link_publisher_credential(
                        session,
                        agent=agent,
                        publisher_cred=publisher_cred,
                        policy=policy,
                    ):
                        report.changed = True
                    report.items.append(
                        CredentialProvisioner._slot_item(
                            parsed,
                            slot=parsed.service_uri,
                            outcome="linked_publisher",
                            credential_id=publisher_cred.id,
                        )
                    )
                    continue
                report.degraded = True
                publisher_failed = True
                logger.warning(
                    "Falling back to placeholder for spec '%s' on install %s "
                    "(publisher credential %s unusable)",
                    parsed.name,
                    agent.id,
                    parsed.publisher_credential_id,
                )

            # ── User-provided branch (default) ────────────────────────────
            mode: str = "placeholder"
            selected_credential_id: uuid.UUID | None = None
            if isinstance(user_selection, dict):
                mode = user_selection.get("mode") or "placeholder"
                cred_id_raw = user_selection.get("credential_id")
                if cred_id_raw:
                    try:
                        selected_credential_id = uuid.UUID(str(cred_id_raw))
                    except (ValueError, TypeError):
                        selected_credential_id = None

            # ``publisher_provides`` on a user spec is a no-op echo — the
            # placeholder below covers it.
            if mode == "use_existing" and selected_credential_id:
                selected = session.get(Credential, selected_credential_id)
                # Accept a credential the installer owns OR one explicitly
                # shared with them (e.g. a slot-tagged credential the
                # publisher pre-shared before install).
                if selected and CredentialProvisioner._installer_can_see(
                    session, agent=agent, credential=selected
                ):
                    if CredentialProvisioner._ensure_link(
                        session, agent.id, selected.id
                    ):
                        report.changed = True
                    report.items.append(
                        CredentialProvisioner._slot_item(
                            parsed,
                            slot=parsed.service_uri,
                            outcome="linked_existing",
                            credential_id=selected.id,
                        )
                    )
                    continue
                logger.warning(
                    "Credential %s not owned by or shared with install owner %s "
                    "— falling back to placeholder",
                    selected_credential_id,
                    agent.owner_id,
                )

            try:
                cred_type = CredentialType(parsed.type)
            except ValueError:
                logger.warning(
                    "Unknown credential type '%s' for spec '%s' — skipping",
                    parsed.type,
                    parsed.name,
                )
                continue

            placeholder = CredentialProvisioner._create_placeholder(
                session, agent=agent, parsed=parsed, cred_type=cred_type, policy=policy
            )
            CredentialProvisioner._ensure_link(session, agent.id, placeholder.id)
            report.changed = True
            report.items.append(
                CredentialProvisioner._slot_item(
                    parsed,
                    slot=parsed.service_uri,
                    outcome=(
                        "publisher_unavailable"
                        if publisher_failed
                        else "placeholder_created"
                    ),
                    credential_id=placeholder.id,
                )
            )

    # ── Skill policy ─────────────────────────────────────────────────────

    @staticmethod
    def _provision_by_slot(
        session: Session,
        *,
        agent: Agent,
        specs: list[ParsedCredentialSpec],
        publisher: PublisherBoundary | None,
        policy: ProvisionPolicy,
        report: ProvisionReport,
    ) -> None:
        """Run the slot decision tree for every spec and apply its writes."""
        linked = CredentialProvisioner._linked_credentials(session, agent.id)
        for parsed in specs:
            cred_type = CredentialProvisioner._slot_credential_type(parsed, log=True)
            if cred_type is None:
                continue
            decision = CredentialProvisioner._decide_slot(
                session,
                agent=agent,
                parsed=parsed,
                cred_type=cred_type,
                publisher=publisher,
                linked=linked,
                log=True,
            )
            if decision.publisher_failed:
                report.degraded = True
                logger.warning(
                    "Publisher credential for slot '%s' unusable on agent %s — "
                    "falling back to a slot credential",
                    spec_slot(parsed),
                    agent.id,
                )

            outcome = decision.outcome
            credential = decision.credential
            if outcome == "linked_publisher" and credential is not None:
                if CredentialProvisioner._share_and_link_publisher_credential(
                    session, agent=agent, publisher_cred=credential, policy=policy
                ):
                    report.changed = True
            elif outcome == "linked_existing" and credential is not None:
                if CredentialProvisioner._ensure_link(session, agent.id, credential.id):
                    report.changed = True
            elif outcome == "template_materialised":
                credential = CredentialProvisioner._materialise_template(
                    session, agent=agent, parsed=parsed, policy=policy
                )
                if credential is None:
                    report.degraded = True
                    logger.warning(
                        "Failed to materialise template credential for slot '%s' "
                        "on agent %s — falling back to placeholder",
                        spec_slot(parsed),
                        agent.id,
                    )
                    outcome = "placeholder_created"
            if outcome in ("placeholder_created", "publisher_unavailable"):
                credential = CredentialProvisioner._create_placeholder(
                    session,
                    agent=agent,
                    parsed=parsed,
                    cred_type=cred_type,
                    policy=policy,
                )
            if (
                outcome
                in (
                    "template_materialised",
                    "placeholder_created",
                    "publisher_unavailable",
                )
                and credential is not None
            ):
                CredentialProvisioner._ensure_link(session, agent.id, credential.id)
                report.changed = True

            if credential is not None and all(
                existing.id != credential.id for existing in linked
            ):
                linked.append(credential)
            report.items.append(
                CredentialProvisioner._slot_item(
                    parsed,
                    slot=spec_slot(parsed),
                    outcome=outcome,
                    credential_id=credential.id if credential is not None else None,
                )
            )

    @staticmethod
    def _decide_slot(
        session: Session,
        *,
        agent: Agent,
        parsed: ParsedCredentialSpec,
        cred_type: CredentialType,
        publisher: PublisherBoundary | None,
        linked: list[Credential],
        log: bool,
    ) -> _SlotDecision:
        """The skill decision tree for one spec. Reads only.

        1. The agent already links a credential of this type carrying the
           slot → ``already_linked``; prefer a filled credential when more
           than one matches, as the runtime slot lookup does.
        2. ``publisher`` spec with a usable publisher credential →
           ``linked_publisher``; otherwise remember the failure.
        3. The installer owns, or has a share on, a credential carrying the
           slot (:meth:`CredentialsService.find_slot_match`) →
           ``linked_existing``.
        4. ``template`` spec → ``template_materialised``; otherwise a
           placeholder (``publisher_unavailable`` after a publisher failure).
        """
        slot = spec_slot(parsed)
        for credential in sorted(linked, key=lambda item: item.is_placeholder):
            if credential.type == cred_type and credential.service_uri == slot:
                return _SlotDecision("already_linked", credential)

        publisher_failed = False
        if parsed.provided_by == "publisher":
            publisher_cred: Credential | None = None
            if parsed.publisher_credential_id is None:
                if log:
                    logger.warning(
                        "Publisher spec for slot '%s' on agent %s carries no "
                        "publisher credential id",
                        slot,
                        agent.id,
                    )
            else:
                publisher_cred = CredentialProvisioner._usable_publisher_credential(
                    session,
                    agent=agent,
                    credential_id=parsed.publisher_credential_id,
                    spec_name=parsed.name,
                    publisher=publisher,
                    log=log,
                )
            # A frozen publisher id is not a runtime slot alias. The payload
            # carries the credential's current type and service_uri, so a
            # publisher who changes either can no longer provide this slot.
            # Keep this check skill-only: bundle resolution is unchanged.
            if publisher_cred is not None and (
                publisher_cred.type != cred_type or publisher_cred.service_uri != slot
            ):
                publisher_cred = None
            if publisher_cred is not None:
                return _SlotDecision("linked_publisher", publisher_cred)
            publisher_failed = True

        own = CredentialsService.find_slot_match(
            session,
            user_id=agent.owner_id,
            credential_type=cred_type,
            service_uri=slot,
        )
        # A foreign publisher credential that just failed its checks (sharing
        # off, owner ≠ package publisher) must not come back through a share
        # as the installer's own: the slot gets a placeholder instead. An
        # installer-owned hit stays usable.
        stale_publisher_share = (
            publisher_failed
            and own is not None
            and own.id == parsed.publisher_credential_id
            and own.owner_id != agent.owner_id
        )
        if own is not None and not stale_publisher_share:
            return _SlotDecision("linked_existing", own, publisher_failed)
        if parsed.provided_by == "template":
            return _SlotDecision("template_materialised", None)
        return _SlotDecision(
            "publisher_unavailable" if publisher_failed else "placeholder_created",
            None,
            publisher_failed,
        )

    @staticmethod
    def _slot_credential_type(
        parsed: ParsedCredentialSpec, *, log: bool
    ) -> CredentialType | None:
        try:
            return CredentialType(parsed.type)
        except ValueError:
            if log:
                logger.warning(
                    "Unknown credential type '%s' for slot '%s' — skipping",
                    parsed.type,
                    spec_slot(parsed),
                )
            return None

    @staticmethod
    def _slot_keys(
        specs: list[ParsedCredentialSpec],
    ) -> set[tuple[CredentialType, str]]:
        keys: set[tuple[CredentialType, str]] = set()
        for parsed in specs:
            cred_type = CredentialProvisioner._slot_credential_type(parsed, log=False)
            if cred_type is not None:
                keys.add((cred_type, spec_slot(parsed)))
        return keys

    # ── Shared writers and checks ────────────────────────────────────────

    @staticmethod
    def _usable_publisher_credential(
        session: Session,
        *,
        agent: Agent,
        credential_id: uuid.UUID,
        spec_name: str,
        publisher: PublisherBoundary | None,
        log: bool,
    ) -> Credential | None:
        """The publisher credential when it still exists, still allows sharing
        and is owned by the package publisher; ``None`` otherwise.

        With ``publisher=None`` the ownership check is skipped (I7, D2).
        """
        publisher_cred = session.get(Credential, credential_id)
        if publisher_cred is None:
            if log:
                logger.warning(
                    "Publisher credential %s missing for spec '%s' on install %s",
                    credential_id,
                    spec_name,
                    agent.id,
                )
            return None
        if not publisher_cred.allow_sharing:
            if log:
                logger.warning(
                    "Publisher credential %s no longer allows sharing for spec '%s' "
                    "on install %s",
                    credential_id,
                    spec_name,
                    agent.id,
                )
            return None
        # Package publisher == credential owner is the trust boundary.
        if publisher is not None and publisher_cred.owner_id != publisher.user_id:
            if log:
                logger.warning(
                    "Publisher credential %s not owned by package publisher %s "
                    "(actual owner %s) — falling through for spec '%s'",
                    credential_id,
                    publisher.user_id,
                    publisher_cred.owner_id,
                    spec_name,
                )
            return None
        return publisher_cred

    @staticmethod
    def _share_and_link_publisher_credential(
        session: Session,
        *,
        agent: Agent,
        publisher_cred: Credential,
        policy: ProvisionPolicy,
    ) -> bool:
        """Share a checked publisher credential with the installer and link it.

        No share when the installer is the publisher. First-writer-wins: an
        existing share (e.g. ``source="direct"``) keeps its source. Returns
        whether a share or link row was inserted.
        """
        changed = False
        if publisher_cred.owner_id != agent.owner_id and not (
            CredentialProvisioner._has_share(
                session, credential_id=publisher_cred.id, user_id=agent.owner_id
            )
        ):
            session.add(
                CredentialShare(
                    credential_id=publisher_cred.id,
                    shared_with_user_id=agent.owner_id,
                    shared_by_user_id=publisher_cred.owner_id,
                    access_level="read",
                    source=policy.share_source,
                )
            )
            session.flush()
            changed = True
        if CredentialProvisioner._ensure_link(session, agent.id, publisher_cred.id):
            changed = True
        return changed

    @staticmethod
    def _materialise_template(
        session: Session,
        *,
        agent: Agent,
        parsed: ParsedCredentialSpec,
        policy: ProvisionPolicy,
    ) -> Credential | None:
        """Create an installer-owned placeholder seeded from a template spec.

        The row carries the publisher's non-private values, mirrors
        ``template_private_fields`` so the setup page can highlight what is
        still missing, and copies ``service_uri`` unless the publisher marked
        it private. Returns ``None`` when the spec type is unknown.
        """
        try:
            cred_type = CredentialType(parsed.type)
        except ValueError:
            return None

        service_uri = (
            None
            if "service_uri" in parsed.template_private_fields
            else parsed.service_uri
        )
        credential = Credential(
            owner_id=agent.owner_id,
            name=parsed.name or "template credential",
            type=cred_type,
            notes=parsed.description or policy.template_notes_fallback,
            encrypted_data=encrypt_field(json.dumps(parsed.non_private_template_data)),
            is_placeholder=True,
            allow_sharing=False,
            allow_template_sharing=False,
            template_private_fields=parsed.template_private_fields,
            service_uri=service_uri,
        )
        if policy.placeholder_stamps_slot:
            # A skill slot is public (it is the spec name), so it is always
            # stamped — the row must stay findable by slot (I9).
            credential.service_uri = spec_slot(parsed)
            credential.user_workspace_id = agent.user_workspace_id
        session.add(credential)
        session.flush()
        return credential

    @staticmethod
    def _create_placeholder(
        session: Session,
        *,
        agent: Agent,
        parsed: ParsedCredentialSpec,
        cred_type: CredentialType,
        policy: ProvisionPolicy,
    ) -> Credential:
        name = (
            spec_slot(parsed)
            if policy.placeholder_name_mode == "slot"
            else f"{parsed.name} (placeholder)"
        )
        placeholder = Credential(
            owner_id=agent.owner_id,
            name=name,
            type=cred_type,
            notes=policy.placeholder_notes,
            encrypted_data=encrypt_field(json.dumps({})),
            is_placeholder=True,
            allow_sharing=False,
        )
        if policy.placeholder_stamps_slot:
            placeholder.service_uri = spec_slot(parsed)
            placeholder.user_workspace_id = agent.user_workspace_id
        session.add(placeholder)
        session.flush()
        return placeholder

    @staticmethod
    def _ensure_link(
        session: Session, agent_id: uuid.UUID, credential_id: uuid.UUID
    ) -> bool:
        """Link a credential to an agent unless linked already. The single
        link-insert path (I9). Returns whether a row was inserted."""
        existing_link = session.exec(
            select(AgentCredentialLink).where(
                AgentCredentialLink.agent_id == agent_id,
                AgentCredentialLink.credential_id == credential_id,
            )
        ).first()
        if existing_link is not None:
            return False
        session.add(AgentCredentialLink(agent_id=agent_id, credential_id=credential_id))
        session.flush()
        return True

    @staticmethod
    def _share_row(
        session: Session, *, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> CredentialShare | None:
        return session.exec(
            select(CredentialShare).where(
                CredentialShare.credential_id == credential_id,
                CredentialShare.shared_with_user_id == user_id,
            )
        ).first()

    @staticmethod
    def _has_share(
        session: Session, *, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        return (
            CredentialProvisioner._share_row(
                session, credential_id=credential_id, user_id=user_id
            )
            is not None
        )

    @staticmethod
    def _installer_can_see(
        session: Session, *, agent: Agent, credential: Credential
    ) -> bool:
        """The agent owner owns the credential or holds a share on it."""
        return (
            credential.owner_id == agent.owner_id
            or CredentialProvisioner._has_share(
                session, credential_id=credential.id, user_id=agent.owner_id
            )
        )

    @staticmethod
    def _linked_credentials(session: Session, agent_id: uuid.UUID) -> list[Credential]:
        return list(
            session.exec(
                select(Credential)
                .join(
                    AgentCredentialLink,
                    col(AgentCredentialLink.credential_id) == Credential.id,
                )
                .where(AgentCredentialLink.agent_id == agent_id)
            ).all()
        )

    @staticmethod
    def _slot_item(
        parsed: ParsedCredentialSpec,
        *,
        slot: str | None,
        outcome: SlotOutcome,
        credential_id: uuid.UUID | None,
        is_placeholder: bool | None = None,
    ) -> SlotProvision:
        return SlotProvision(
            spec_name=parsed.name,
            spec_type=parsed.type,
            slot=slot,
            provided_by=parsed.provided_by,
            description=parsed.description,
            outcome=outcome,
            credential_id=credential_id,
            is_placeholder=is_placeholder,
        )
