"""Credential slots of a catalog skill: publish-time resolution and projections.

A skill declares the credentials its scripts need in its ``SKILL.md``
frontmatter (``credentials: [{slot, type, description}]``, validated by
``skill_manifest.parse_credential_declarations``). A slot is a
``Credential.service_uri`` value.

At publish, each declaration is resolved against the credentials **linked to
the publishing agent** (a Desktop upload, which has no agent, names its
publisher-provided slots explicitly: :meth:`resolve_for_desktop_publish`) and
frozen onto the immutable
``SkillPackageRevision.required_credential_specs`` — the same spec schema as a
bundle revision, written by ``credential_spec.build_spec`` and read back by
``credential_spec.parse_credential_spec``.

Resolution uses only the credential's own consent flags and requires the
publisher to **own** the credential for ``publisher`` / ``template``: a share
received by the publisher cannot be re-shared, and install later only shares a
credential whose owner is the package publisher. Bundle-only
``publish_settings.credential_overrides`` never apply here.

After install, :class:`SkillSlotIndex` is the one read model of how an agent's
catalog skills' slots stand: the Addons row status, the readiness gate's D1
exclusion and uninstall's slot release all read it. The Addons projection also
asks it about the slots a local or plugin skill declares in its ``SKILL.md``,
which have no revision behind them.

Imports are one-way: this module reads bundle helpers and the credential
provisioner; the readiness gate imports it lazily, nothing else under
``services/bundles/`` or ``services/credentials/`` imports it.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from sqlmodel import Session, col, select

from app.models.agent_api.agent_api_token import AgentApiKeyStatus
from app.models.agents.agent import Agent
from app.models.credentials.credential import (
    Credential,
    CredentialSkillUsage,
    CredentialType,
)
from app.models.credentials.credential_share import CredentialShare
from app.models.credentials.link_models import AgentCredentialLink
from app.models.plugins.llm_plugin import AgentPluginLink, PluginSource
from app.models.skills.schemas import (
    SkillCredentialProvisionPublic,
    SkillPublishCredentialCandidate,
    SkillPublisherCredentialIn,
    SkillCredentialRequirementPublic,
    SkillPublishCredentialPreview,
)
from app.models.skills.skill_package import SkillPackage
from app.models.skills.skill_package_revision import SkillPackageRevision
from app.models.users.user import User
from app.services.bundles.credential_spec import (
    ParsedCredentialSpec,
    build_spec,
    parse_credential_spec,
)
from app.services.bundles.publish_service import PublishService
from app.services.credentials.credential_provisioner import (
    SlotProvision,
    bundle_claimed_credential_ids,
    credential_type_value,
    spec_slot,
)
from app.services.agent_api.agent_api_key_status import key_states_by_credential
from app.services.credentials.credentials_service import CredentialsService
from app.services.skills.exceptions import SkillCatalogError

logger = logging.getLogger(__name__)

ProvidedBy = Literal["user", "publisher", "template"]

#: Why a slot resolved to ``user``.
REASON_NO_LINKED_CREDENTIAL = "no_linked_credential"
REASON_NOT_SHAREABLE = "not_shareable"
REASON_NOT_OWNED = "not_owned"
#: D12: template sharing is on, but a secret field would ship in the template.
REASON_TEMPLATE_WOULD_LEAK_SECRET = "template_would_leak_secret"

_AGENT_API_TYPE = "agent_api"

#: D12 / I6: the secret keys of each credential type's **stored**
#: ``credential_data`` — the dict ``PublishService._template_payload_for`` copies
#: into ``template_data``. ``CredentialsService.SENSITIVE_FIELDS`` names the
#: **env-shaped** data instead (after ``_process_api_token_credential``), so on
#: its own it misses a stored secret: for ``api_token`` it lists only the
#: computed ``http_header_value``, never the stored ``api_token`` key.
#: Force-private types (OAuth, service account) are omitted: their whole
#: payload is dropped at publish. A new credential type must be added here.
_STORED_SECRET_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "email_imap": frozenset({"password"}),
    "email_smtp": frozenset({"password"}),
    "odoo": frozenset({"api_token"}),
    "api_token": frozenset({"api_token", "http_header_value"}),
    "ssh_key": frozenset({"private_key", "passphrase"}),
    "agent_api": frozenset({"token"}),
    "mcp_provider": frozenset({"token", "oauth_client_secret", "oauth_refresh_token"}),
}

#: D17: secret fields the env sync **computes**, mapped to their stored source.
#: The credential API accepts arbitrary data, so imported payloads can also
#: contain these computed keys. ``build_specs`` strips them explicitly from
#: skill templates; marking the source private is therefore enough. Without
#: this, an ``api_token`` slot could never resolve to
#: ``template``: the private-field picker only offers stored fields, so
#: ``http_header_value`` could never be marked private and always counted as
#: leaking. Each value must itself be classified in
#: ``_STORED_SECRET_FIELDS_BY_TYPE`` (guarded by a unit test).
_DERIVED_SECRET_SOURCES: dict[str, dict[str, str]] = {
    "api_token": {"http_header_value": "api_token"},
}

#: Returned for a type no secret map classifies, so it can never be ``template``.
_UNCLASSIFIED_TYPE_SECRET = "<unclassified credential type>"


@dataclass(frozen=True)
class SkillCredentialResolution:
    """How one declared slot resolves against the publishing agent.

    ``credential`` is the publisher's **own** matched credential — also for a
    ``not_shareable`` or ``template_would_leak_secret`` resolution, so the
    preview can name it — and ``None`` when the publisher owns no match.
    """

    slot: str
    type: str
    description: str | None
    provided_by: ProvidedBy
    credential: Credential | None
    reason: str | None
    producer_agent_id: uuid.UUID | None
    producer_agent_name: str | None = None


class SkillCredentialRequirements:
    """Publish derivation and read projections of skill credential slots."""

    @staticmethod
    def resolve_for_publish(
        session: Session,
        *,
        agent: Agent,
        publisher: User,
        declarations: list[dict[str, Any]],
    ) -> list[SkillCredentialResolution]:
        """Resolve each declaration against ``agent``'s linked credentials.

        Read-only. One query loads every linked credential; candidates for a
        slot are the linked credentials of the declared type whose
        ``service_uri`` equals the slot, newest id first.

        Placeholders are never candidates, owned or not: an unfilled
        placeholder shared as ``publisher`` would hand installers an empty
        credential they cannot edit. A slot whose only linked credentials are
        placeholders therefore resolves to ``user`` / ``no_linked_credential``.
        """
        if not declarations:
            return []

        linked = [
            cred
            for cred in session.exec(
                select(Credential)
                .join(
                    AgentCredentialLink,
                    AgentCredentialLink.credential_id == Credential.id,
                )
                .where(AgentCredentialLink.agent_id == agent.id)
                .order_by(Credential.id.desc())
            ).all()
            if not cred.is_placeholder
        ]

        return [
            SkillCredentialRequirements._resolve_one(
                session, declaration=declaration, linked=linked, publisher=publisher
            )
            for declaration in declarations
        ]

    @staticmethod
    def resolve_for_desktop_publish(
        session: Session,
        *,
        publisher: User,
        declarations: list[dict[str, Any]],
        publisher_credentials: Iterable[SkillPublisherCredentialIn],
    ) -> list[SkillCredentialResolution]:
        """Resolve a desktop publish, which has no agent to link credentials.

        Nothing is inferred: a desktop has no link step, so an owned,
        shareable credential carrying the slot would otherwise go to every
        user of the package unasked. Only a slot named in
        ``publisher_credentials`` resolves to ``publisher`` / ``template``, and
        only against exactly that credential (the caller's own, filled, the
        declared type, ``service_uri == slot``, and ``allow_sharing`` →
        ``publisher`` or a non-leaking ``allow_template_sharing`` →
        ``template``). Every other slot is ``user``.

        Raises:
            SkillCatalogError: ``invalid_publisher_credential`` for a choice
                naming an undeclared or repeated slot, or a credential that
                cannot provide it.
        """
        declared = {declaration["slot"]: declaration for declaration in declarations}
        chosen: dict[str, uuid.UUID] = {}
        for choice in publisher_credentials:
            if choice.slot not in declared:
                raise _invalid_publisher_credential(
                    f"The skill declares no credential slot '{choice.slot}'."
                )
            if choice.slot in chosen:
                raise _invalid_publisher_credential(
                    f"The credential slot '{choice.slot}' is named twice."
                )
            chosen[choice.slot] = choice.credential_id

        resolutions: list[SkillCredentialResolution] = []
        for declaration in declarations:
            slot = declaration["slot"]
            if slot not in chosen:
                resolutions.append(_user_resolution(declaration))
                continue
            credential = session.get(Credential, chosen[slot])
            if (
                credential is None
                or credential.owner_id != publisher.id
                or credential.is_placeholder
                or credential_type_value(credential) != declaration["type"]
                or credential.service_uri != slot
            ):
                raise _invalid_publisher_credential(
                    f"The credential for slot '{slot}' must be one of your own "
                    f"filled {declaration['type']} credentials whose slot is "
                    f"'{slot}'."
                )
            resolution = SkillCredentialRequirements._resolve_one(
                session, declaration=declaration, linked=[credential], publisher=publisher
            )
            if resolution.provided_by == "user":
                raise _invalid_publisher_credential(
                    f"The credential for slot '{slot}' allows neither sharing nor "
                    "a template without secrets."
                    if resolution.reason != REASON_TEMPLATE_WOULD_LEAK_SECRET
                    else f"A template of the credential for slot '{slot}' would "
                    "carry a secret field. Mark it private, or allow sharing."
                )
            resolutions.append(resolution)
        return resolutions

    @staticmethod
    def desktop_publish_candidates(
        session: Session, *, publisher: User, declarations: list[dict[str, Any]]
    ) -> dict[str, list[SkillPublishCredentialCandidate]]:
        """Per declared slot, the caller's own credentials that may be named
        for it in ``publisher_credentials``, and as what. Read-only."""
        slots = {declaration["slot"] for declaration in declarations}
        if not slots:
            return {}
        owned = session.exec(
            select(Credential)
            .where(
                Credential.owner_id == publisher.id,
                col(Credential.service_uri).in_(slots),
            )
            .order_by(Credential.id.desc())
        ).all()
        candidates: dict[str, list[SkillPublishCredentialCandidate]] = {}
        for declaration in declarations:
            slot, credential_type = declaration["slot"], declaration["type"]
            for credential in owned:
                if (
                    credential.is_placeholder
                    or credential.service_uri != slot
                    or credential_type_value(credential) != credential_type
                ):
                    continue
                options: list[Literal["publisher", "template"]] = []
                if credential.allow_sharing:
                    options.append("publisher")
                if (
                    credential.allow_template_sharing
                    and credential_type != _AGENT_API_TYPE
                    and not SkillCredentialRequirements._template_leaking_secret_fields(
                        credential, credential_type
                    )
                ):
                    options.append("template")
                if options:
                    candidates.setdefault(slot, []).append(
                        SkillPublishCredentialCandidate(
                            id=credential.id,
                            name=credential.name,
                            type=credential_type,
                            can_provide_as=options,
                        )
                    )
        return candidates

    @staticmethod
    def _resolve_one(
        session: Session,
        *,
        declaration: dict[str, Any],
        linked: list[Credential],
        publisher: User,
    ) -> SkillCredentialResolution:
        slot = declaration["slot"]
        credential_type = declaration["type"]
        candidates = [
            cred
            for cred in linked
            if credential_type_value(cred) == credential_type and cred.service_uri == slot
        ]
        owned = [cred for cred in candidates if cred.owner_id == publisher.id]

        provided_by: ProvidedBy = "user"
        reason: str | None = None
        match: Credential | None = next(
            (cred for cred in owned if cred.allow_sharing), None
        )
        if match is not None:
            provided_by = "publisher"
        else:
            # D12: a connection has no user-fillable private fields, so an
            # ``agent_api`` slot is never offered as a template.
            templatable = [
                cred
                for cred in owned
                if cred.allow_template_sharing and credential_type != _AGENT_API_TYPE
            ]
            match = next(
                (
                    cred
                    for cred in templatable
                    if not SkillCredentialRequirements._template_leaking_secret_fields(
                        cred, credential_type
                    )
                ),
                None,
            )
            if match is not None:
                provided_by = "template"
            elif templatable:
                match = templatable[0]
                reason = REASON_TEMPLATE_WOULD_LEAK_SECRET
            elif owned:
                match = owned[0]
                reason = REASON_NOT_SHAREABLE
            elif candidates:
                reason = REASON_NOT_OWNED
            else:
                reason = REASON_NO_LINKED_CREDENTIAL

        producer_agent_id = (
            SkillCredentialRequirements._read_producer_agent_id(session, match)
            if match is not None and credential_type == _AGENT_API_TYPE
            else None
        )
        producer_agent_name = (
            SkillCredentialRequirements._read_producer_agent_name(
                session, producer_agent_id
            )
            if producer_agent_id is not None
            else None
        )

        # A credential's notes are exposed only when its owner consented to
        # provide the credential (``publisher`` / ``template``). Otherwise a
        # non-shared credential's free-text notes would be frozen into a
        # revision visible to every catalog viewer.
        fallback_description = (
            (match.notes or None)
            if match is not None and provided_by in ("publisher", "template")
            else None
        )
        return SkillCredentialResolution(
            slot=slot,
            type=credential_type,
            description=declaration.get("description") or fallback_description,
            provided_by=provided_by,
            credential=match,
            reason=reason,
            producer_agent_id=producer_agent_id,
            producer_agent_name=producer_agent_name,
        )

    @staticmethod
    def _template_leaking_secret_fields(
        credential: Credential, credential_type: str
    ) -> frozenset[str]:
        """Secret fields a template of ``credential`` would still carry (D12).

        Decided on field names alone, with no decryption, by mirroring what
        ``PublishService._template_payload_for`` strips: a force-private type
        drops the whole payload, a per-type allowlist drops every other field,
        and ``template_private_fields`` drops what the owner marked private.
        A secret name left over would be frozen into an immutable revision
        that every catalog viewer can read, and copied into every installer's
        credential, so a non-empty result means the slot must not resolve to
        ``template``.

        The secret names are ``CredentialsService.SENSITIVE_FIELDS[type]``
        (env-shaped names) united with ``_STORED_SECRET_FIELDS_BY_TYPE[type]``
        (stored ``credential_data`` keys, which ``_template_payload_for``
        actually copies). ``SENSITIVE_FIELDS`` alone let an ``api_token``
        credential marked private only on ``http_header_value`` freeze its raw
        ``api_token``. Coordinator ruling for Phase 2, implementing D12 / I6.
        Fails closed: a type that is not force-private and that neither map
        classifies always leaks.

        A field ``_DERIVED_SECRET_SOURCES`` names is computed at env-sync time
        and explicitly stripped by ``build_specs`` even if an imported payload
        stores it. Its source still has to be protected (D17). Without that,
        an ``api_token`` could never be a template —
        the private-field picker only offers stored fields, so the computed
        ``http_header_value`` could never be marked private.

        Bundle ``_template_payload_for`` has the same gap. Fixing it is out of
        scope (plan §13) and it stays unchanged (I1).
        """
        if credential_type in PublishService._TEMPLATE_FORCE_PRIVATE_TYPES:
            return frozenset()
        stored_secrets = _STORED_SECRET_FIELDS_BY_TYPE.get(credential_type)
        env_secrets = CredentialsService.SENSITIVE_FIELDS.get(credential_type)
        if stored_secrets is None and env_secrets is None:
            return frozenset({_UNCLASSIFIED_TYPE_SECRET})
        secrets = set(env_secrets or []) | set(stored_secrets or ())
        allowlist = PublishService._TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE.get(
            credential_type
        )
        private = {
            field
            for field in (credential.template_private_fields or [])
            if isinstance(field, str)
        }

        def stripped(field: str) -> bool:
            return field in private or (allowlist is not None and field not in allowlist)

        derived = _DERIVED_SECRET_SOURCES.get(credential_type, {})
        return frozenset(
            field
            for field in secrets
            if not stripped(field)
            and not (field in derived and stripped(derived[field]))
        )

    @staticmethod
    def _read_producer_agent_id(
        session: Session, credential: Credential
    ) -> uuid.UUID | None:
        """The producer agent of an ``agent_api`` connection, or ``None``.

        Informational only, so it never fails a publish: an undecryptable
        credential or a malformed id both read as "unknown".
        """
        try:
            data = CredentialsService.decrypt_credential_data(
                session=session, credential=credential
            )
        except Exception:
            logger.warning(
                "skill_publish_producer_agent_id_unreadable credential_id=%s",
                credential.id,
            )
            return None
        raw = data.get("producer_agent_id") if isinstance(data, dict) else None
        if raw is None:
            return None
        try:
            return uuid.UUID(str(raw))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _read_producer_agent_name(
        session: Session, producer_agent_id: uuid.UUID
    ) -> str | None:
        """The producer agent's display name, or ``None`` if it is gone.

        Informational like the id it accompanies: no access check, because the
        name of the agent a published skill talks to is already carried by the
        slot itself (the slot *is* the connection's ``service_uri``).
        """
        return session.exec(
            select(Agent.name).where(Agent.id == producer_agent_id)
        ).first()

    @staticmethod
    def build_specs(
        session: Session, resolutions: list[SkillCredentialResolution]
    ) -> list[dict]:
        """Freeze resolutions into ``required_credential_specs`` entries.

        The slot is both the spec ``name`` and its ``service_uri``. A ``user``
        spec never points at the publisher's credential, even when one matched.

        Raises:
            ValueError: from ``PublishService._template_payload_for`` when a
                template credential's stored data cannot be decrypted.
        """
        specs: list[dict] = []
        for resolution in resolutions:
            spec = build_spec(
                session,
                credential=(
                    resolution.credential
                    if resolution.provided_by != "user"
                    else None
                ),
                credential_type=resolution.type,
                provided_by=resolution.provided_by,
                name=resolution.slot,
                service_uri=resolution.slot,
                description=resolution.description,
            )
            if resolution.provided_by == "template":
                # The API accepts arbitrary credential_data. A stored copy
                # of a computed secret must not survive the D17 waiver when
                # its source is private. Keep this skill-only (bundle I1).
                for field in _DERIVED_SECRET_SOURCES.get(resolution.type, {}):
                    spec["template_data"].pop(field, None)
            # Appended by this caller only, never by ``build_spec``, so bundle
            # revision JSON keeps its exact shape.
            if (
                resolution.type == _AGENT_API_TYPE
                and resolution.producer_agent_id is not None
            ):
                spec["producer_agent_id"] = str(resolution.producer_agent_id)
                if resolution.producer_agent_name is not None:
                    spec["producer_agent_name"] = resolution.producer_agent_name
            specs.append(spec)
        return specs

    @staticmethod
    def to_publish_preview(
        resolutions: list[SkillCredentialResolution],
    ) -> list[SkillPublishCredentialPreview]:
        """Project resolutions for the publisher's Share dialog."""
        return [
            SkillPublishCredentialPreview(
                slot=resolution.slot,
                type=resolution.type,
                description=resolution.description,
                provided_by=resolution.provided_by,
                credential_id=(
                    resolution.credential.id if resolution.credential else None
                ),
                credential_name=(
                    resolution.credential.name if resolution.credential else None
                ),
                producer_agent_id=resolution.producer_agent_id,
                producer_agent_name=resolution.producer_agent_name,
                reason=resolution.reason,
            )
            for resolution in resolutions
        ]

    @staticmethod
    def parse_specs(raw_specs: object) -> list[ParsedCredentialSpec]:
        """A revision's frozen specs, parsed; unparseable entries are skipped."""
        if not isinstance(raw_specs, list):
            return []
        return [
            parsed
            for raw in raw_specs
            if (parsed := parse_credential_spec(raw)) is not None
        ]

    @staticmethod
    def provisions_to_public(
        session: Session,
        *,
        agent: Agent,
        items: list[SlotProvision],
    ) -> list[SkillCredentialProvisionPublic]:
        """Project provisioning items for the install response and preview.

        ``credential_name`` is filled only for a credential the agent owner
        owns or already holds a share on, so a publisher credential is never
        named to an installer before its share exists (C3). Three queries at
        most (credentials, direct shares, group shares), whatever the number
        of slots.
        """
        credential_ids = {
            item.credential_id for item in items if item.credential_id is not None
        }
        visible_names: dict[uuid.UUID, str] = {}
        credentials_by_id: dict[uuid.UUID, Credential] = {}
        shared_ids: set[uuid.UUID] = set()
        if credential_ids:
            credentials = session.exec(
                select(Credential).where(col(Credential.id).in_(credential_ids))
            ).all()
            credentials_by_id = {credential.id: credential for credential in credentials}
            foreign_ids = [
                credential.id
                for credential in credentials
                if credential.owner_id != agent.owner_id
            ]
            shared_ids = _shared_with(
                session, credential_ids=foreign_ids, user_id=agent.owner_id
            )
            visible_names = {
                credential.id: credential.name
                for credential in credentials
                if credential.owner_id == agent.owner_id
                or credential.id in shared_ids
            }

        def needs_setup(item: SlotProvision) -> bool:
            if item.outcome == "linked_publisher" and item.credential_id is None:
                # Preview deliberately redacts the id before sharing. Its
                # decision already checked consent; only configuration remains.
                return item.is_placeholder is not False
            if item.outcome in (
                "template_materialised",
                "placeholder_created",
                "publisher_unavailable",
            ):
                return True
            credential = (
                credentials_by_id.get(item.credential_id)
                if item.credential_id is not None
                else None
            )
            if credential is None or credential.is_placeholder:
                return True
            if credential.owner_id == agent.owner_id:
                return False
            # A previewed publisher share does not exist yet. Other linked
            # outcomes must already have access, as in SkillSlotIndex.
            return not credential.allow_sharing or (
                item.outcome != "linked_publisher" and credential.id not in shared_ids
            )

        return [
            SkillCredentialProvisionPublic(
                slot=item.slot or item.spec_name,
                type=item.spec_type,
                description=item.description,
                provided_by=item.provided_by,
                outcome=item.outcome,
                needs_setup=needs_setup(item),
                credential_id=item.credential_id,
                credential_name=(
                    visible_names.get(item.credential_id)
                    if item.credential_id is not None
                    else None
                ),
            )
            for item in items
        ]

    @staticmethod
    def publisher_usages_of_credential(
        session: Session,
        *,
        credential_id: uuid.UUID,
        publisher_user_id: uuid.UUID,
    ) -> tuple[list[CredentialSkillUsage], list[uuid.UUID]]:
        """Packages of ``publisher_user_id`` providing ``credential_id``.

        A revision counts when one of its frozen specs is
        ``provided_by="publisher"`` with this ``publisher_credential_id``.
        Returns the usages grouped by package plus **every** revision id of the
        packages those revisions belong to — see the loop below for why the
        second element is wider than the first. Two queries.
        """
        packages = session.exec(
            select(SkillPackage)
            .where(SkillPackage.publisher_user_id == publisher_user_id)
            .order_by(SkillPackage.display_name)
        ).all()
        if not packages:
            return [], []

        revision_rows = session.exec(
            select(
                SkillPackageRevision.id,
                SkillPackageRevision.package_id,
                SkillPackageRevision.revision_number,
                SkillPackageRevision.required_credential_specs,
            )
            .where(
                col(SkillPackageRevision.package_id).in_(
                    [package.id for package in packages]
                )
            )
            .order_by(SkillPackageRevision.revision_number)
        ).all()

        revision_numbers: dict[uuid.UUID, list[int]] = {}
        revisions_by_package: dict[uuid.UUID, list[uuid.UUID]] = {}
        for revision_id, package_id, revision_number, raw_specs in revision_rows:
            revisions_by_package.setdefault(package_id, []).append(revision_id)
            if any(
                parsed.provided_by == "publisher"
                and parsed.publisher_credential_id == credential_id
                for parsed in SkillCredentialRequirements.parse_specs(raw_specs)
            ):
                revision_numbers.setdefault(package_id, []).append(revision_number)

        # An install of *any* revision of an affected package counts, not only
        # of the revisions that still provide the credential. Upgrading re-pins
        # the link and provisions the specs a revision *added*; it never
        # releases one it dropped. So an installer who has moved on to a
        # revision where the slot became user-provided still holds the share and
        # the link, and deleting the credential still breaks them. Matching on
        # the providing revisions alone lost exactly those installers.
        revision_ids = [
            revision_id
            for package_id in revision_numbers
            for revision_id in revisions_by_package[package_id]
        ]

        usages = [
            CredentialSkillUsage(
                package_uuid=package.id,
                package_id=package.package_id,
                display_name=package.display_name,
                revision_numbers=revision_numbers[package.id],
            )
            for package in packages
            if package.id in revision_numbers
        ]
        return usages, revision_ids

    @staticmethod
    def specs_to_public(
        raw_specs: list | None,
    ) -> list[SkillCredentialRequirementPublic]:
        """Project a revision's frozen specs; unparseable entries are skipped."""
        requirements: list[SkillCredentialRequirementPublic] = []
        for parsed in SkillCredentialRequirements.parse_specs(raw_specs):
            slot = parsed.service_uri or parsed.name
            if not isinstance(slot, str) or not isinstance(parsed.type, str):
                continue
            requirements.append(
                SkillCredentialRequirementPublic(
                    slot=slot,
                    type=parsed.type,
                    description=parsed.description,
                    provided_by=parsed.provided_by,
                    publisher_credential_id=parsed.publisher_credential_id,
                    producer_agent_id=parsed.producer_agent_id,
                    producer_agent_name=parsed.producer_agent_name,
                )
            )
        return requirements


CredentialIssueReason = Literal["not_linked", "not_configured", "access_revoked"]

#: The spec types a slot can be provisioned for; the provisioner skips others.
_CREDENTIAL_TYPE_VALUES = frozenset(member.value for member in CredentialType)


@dataclass(frozen=True)
class CredentialIssue:
    """One skill slot the agent cannot use yet."""

    slot: str
    type: str
    reason: CredentialIssueReason


@dataclass(frozen=True)
class _SlotState:
    #: Linked credentials of the spec's type carrying its current slot.
    candidates: tuple[Credential, ...]
    #: ``None`` when a candidate is usable.
    reason: CredentialIssueReason | None


class SkillSlotIndex:
    """How the credential slots of an agent's catalog skills stand.

    Built from the frozen specs of the revisions the agent's ``source=catalog``
    links pin — never from the environment's skill index, which a pre-feature
    container reports without credentials — and from the agent's linked
    credentials. One instance answers the Addons row status, the readiness
    gate's D1 exclusion and uninstall's released/retained split.

    A spec is satisfied when one of its candidates is filled in and either
    owned by the agent owner, or foreign, still ``allow_sharing`` **and** shared
    with the agent owner. A share row alone is not enough: older sharing-disable
    paths could leave it behind. Otherwise the reason is ``not_linked`` (no
    candidate), ``not_configured`` (a placeholder) or
    ``access_revoked``. An issued agent API key is usable only while its key
    status is ``active``: a revoked, expired, paused or orphaned key leaves its
    slot ``access_revoked`` (``inactive_key_ids``).

    The same reasons judge the ``(slot, type)`` pairs a local or plugin skill
    declares (:meth:`issues_for_declarations`). Those have no revision, so they
    come from the environment's skill index — the only record of a workspace
    ``SKILL.md`` — and a pre-feature container simply reports none.
    """

    def __init__(
        self,
        *,
        owner_id: uuid.UUID,
        specs_by_link: dict[uuid.UUID, list[ParsedCredentialSpec]],
        linked: list[Credential],
        shared_ids: set[uuid.UUID],
        bundle_claimed_ids: set[uuid.UUID],
        inactive_key_ids: set[uuid.UUID] | None = None,
    ) -> None:
        self._owner_id = owner_id
        self._specs_by_link = specs_by_link
        self._linked = linked
        self._shared_ids = shared_ids
        self._bundle_claimed_ids = frozenset(bundle_claimed_ids)
        self._inactive_key_ids = frozenset(inactive_key_ids or ())

    @classmethod
    def build_for_agent(
        cls, session: Session, agent: Agent, *, with_declarations: bool = False
    ) -> SkillSlotIndex:
        """Load the index with a constant number of queries (I11).

        The agent's catalog links, their revisions' specs, the agent's linked
        credentials, the share rows of the foreign ones with the agent owner,
        and — only for an agent with an installed bundle revision — that
        revision. The last three are skipped when no catalog link declares a
        slot, unless ``with_declarations`` says the caller will also judge
        declarations read from the skill index
        (:meth:`issues_for_declarations`).
        """
        links = session.exec(
            select(AgentPluginLink.id, AgentPluginLink.skill_package_revision_id).where(
                AgentPluginLink.agent_id == agent.id,
                AgentPluginLink.source == PluginSource.catalog,
            )
        ).all()
        revision_ids = {
            revision_id for _, revision_id in links if revision_id is not None
        }
        raw_specs_by_revision: dict[uuid.UUID, object] = {}
        if revision_ids:
            raw_specs_by_revision = dict(
                session.exec(
                    select(
                        SkillPackageRevision.id,
                        SkillPackageRevision.required_credential_specs,
                    ).where(col(SkillPackageRevision.id).in_(revision_ids))
                ).all()
            )
        specs_by_link = {
            link_id: SkillCredentialRequirements.parse_specs(
                raw_specs_by_revision.get(revision_id) if revision_id else None
            )
            for link_id, revision_id in links
        }
        if not with_declarations and not any(specs_by_link.values()):
            return cls(
                owner_id=agent.owner_id,
                specs_by_link=specs_by_link,
                linked=[],
                shared_ids=set(),
                bundle_claimed_ids=set(),
            )

        linked = list(
            session.exec(
                select(Credential)
                .join(
                    AgentCredentialLink,
                    col(AgentCredentialLink.credential_id) == Credential.id,
                )
                .where(AgentCredentialLink.agent_id == agent.id)
            ).all()
        )
        return cls(
            owner_id=agent.owner_id,
            specs_by_link=specs_by_link,
            linked=linked,
            shared_ids=_shared_with(
                session,
                credential_ids=[
                    credential.id
                    for credential in linked
                    if credential.owner_id != agent.owner_id
                ],
                user_id=agent.owner_id,
            ),
            bundle_claimed_ids=bundle_claimed_credential_ids(
                session, agent=agent, linked_credentials=linked
            ),
            inactive_key_ids=_inactive_issued_key_ids(session, linked),
        )

    def specs_for_link(self, link_id: uuid.UUID) -> list[ParsedCredentialSpec]:
        return list(self._specs_by_link.get(link_id, []))

    def specs_except_link(self, link_id: uuid.UUID) -> list[ParsedCredentialSpec]:
        """The specs of every other catalog link on the agent."""
        return [
            parsed
            for other_link_id, specs in self._specs_by_link.items()
            if other_link_id != link_id
            for parsed in specs
        ]

    def issues_for_link(self, link_id: uuid.UUID) -> list[CredentialIssue]:
        return self.issues_for_declarations(
            (spec_slot(parsed), parsed.type)
            for parsed in self._specs_by_link.get(link_id, [])
        )

    def issues_for_declarations(
        self, declarations: Iterable[tuple[str, str]]
    ) -> list[CredentialIssue]:
        """The unusable ones among ``(slot, type)`` declarations, in order.

        Also serves skills with no pinned revision — a local skill, a
        marketplace or bundle plugin's skill — whose ``SKILL.md`` declarations
        reach the backend only through the environment's skill index. Such an
        index must be built ``with_declarations=True``: otherwise no linked
        credential is loaded and every slot reads ``not_linked``.
        """
        issues: list[CredentialIssue] = []
        for slot, credential_type in declarations:
            state = self._slot_state(slot, credential_type)
            if state is not None and state.reason is not None:
                issues.append(
                    CredentialIssue(slot=slot, type=credential_type, reason=state.reason)
                )
        return issues

    def slot_credential_ids(self) -> set[uuid.UUID]:
        """Every linked credential a catalog skill spec on the agent points at.

        No bundle subtraction: this answers "does anything installed still
        declare this slot", which is what stale copy has to be checked against.
        """
        ids: set[uuid.UUID] = set()
        for specs in self._specs_by_link.values():
            for parsed in specs:
                state = self._slot_state(spec_slot(parsed), parsed.type)
                if state is not None:
                    ids.update(credential.id for credential in state.candidates)
        return ids

    def skill_provisioned_credential_ids(self) -> set[uuid.UUID]:
        """Credentials linked because a catalog skill spec requires them (D1).

        Every candidate of every catalog spec, minus the credentials the
        agent's bundle revision claims (I10).
        """
        return self.slot_credential_ids() - self._bundle_claimed_ids

    def _slot_state(self, slot: str, credential_type: str) -> _SlotState | None:
        """Candidates and satisfaction of one slot; ``None`` for an unknown type."""
        if credential_type not in _CREDENTIAL_TYPE_VALUES:
            return None
        candidates = tuple(
            credential
            for credential in self._linked
            if credential_type_value(credential) == credential_type
            and credential.service_uri == slot
        )
        reason: CredentialIssueReason | None
        if any(self._usable(credential) for credential in candidates):
            reason = None
        elif not candidates:
            reason = "not_linked"
        elif any(
            credential.is_placeholder
            for credential in candidates
        ):
            reason = "not_configured"
        else:
            reason = "access_revoked"
        return _SlotState(candidates=candidates, reason=reason)

    def _usable(self, credential: Credential) -> bool:
        if credential.is_placeholder:
            return False
        if credential.id in self._inactive_key_ids:
            return False
        if credential.owner_id == self._owner_id:
            return True
        return bool(credential.allow_sharing) and credential.id in self._shared_ids


def _inactive_issued_key_ids(
    session: Session, linked: list[Credential]
) -> set[uuid.UUID]:
    """Linked issued agent API keys whose key status is not ``active``.

    One batched read over the ``agent_api`` credentials; connections (no
    external key) are absent from the key states and stay usable.
    """
    agent_api_ids = [
        credential.id
        for credential in linked
        if credential_type_value(credential) == CredentialType.AGENT_API.value
    ]
    if not agent_api_ids:
        return set()
    return {
        credential_id
        for credential_id, state in key_states_by_credential(
            session, agent_api_ids
        ).items()
        if state.status != AgentApiKeyStatus.ACTIVE
    }


def _shared_with(
    session: Session, *, credential_ids: list[uuid.UUID], user_id: uuid.UUID
) -> set[uuid.UUID]:
    """Ids among ``credential_ids`` shared with ``user_id``, directly or through a
    user group. No query when empty."""
    if not credential_ids:
        return set()
    from app.services.credentials.credential_share_service import (
        CredentialShareService,
    )

    direct = set(
        session.exec(
            select(CredentialShare.credential_id).where(
                col(CredentialShare.credential_id).in_(credential_ids),
                CredentialShare.shared_with_user_id == user_id,
            )
        ).all()
    )
    return direct | CredentialShareService.group_shared_ids(
        session, user_id=user_id, credential_ids=credential_ids
    )


def _user_resolution(declaration: dict[str, Any]) -> SkillCredentialResolution:
    """A slot each installer fills (``provided_by='user'``)."""
    return SkillCredentialResolution(
        slot=declaration["slot"],
        type=declaration["type"],
        description=declaration.get("description"),
        provided_by="user",
        credential=None,
        reason=None,
        producer_agent_id=None,
    )


def _invalid_publisher_credential(message: str) -> SkillCatalogError:
    return SkillCatalogError("invalid_publisher_credential", message)
