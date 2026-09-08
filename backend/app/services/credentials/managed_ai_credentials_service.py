"""
Managed AI Credentials Service.

Owns the **parent** ``ManagedAICredential`` record, the **membership** rows that
say who holds it, and the **reconcile** routine that diffs a desired target-user
set against those rows.

Children remain ordinary per-user ``AICredential`` rows that look EXACTLY like
today's admin-managed credentials to the rest of the system. This service does
NOT duplicate encryption, per-type validation, one-default-per-type, profile
auto-sync, or blast-radius logic — every per-child create/update/delete/
set-default is delegated to :data:`ai_credentials_service`. After delegating to
``create_credential`` the new child is stamped with ``is_admin_managed=True``,
``managed_by_id=parent.managed_by_id`` and ``managed_credential_id=parent.id`` so
it is structurally linked back to the parent.

MEMBERSHIP IS A ROW
-------------------
``managed_ai_credential_membership`` holds one row per (parent, user), and it is
the **only** definition of "who is a member". It used to be derived from the
children; that derivation is gone rather than kept alongside, because two
definitions of membership is the duplication this whole phase exists to remove.
The child credential is now a *consequence* of membership — present when a key
exists, absent while one is being minted or after a mint has failed — and
``membership.status`` is what says which.

WHO WRITES THE MEMBERSHIP ROW
-----------------------------
Split by question, so the two writers cannot disagree:

- **This service owns membership existence** — a row appears when an admin adds
  someone, and disappears when they are removed. It also sets the *initial*
  status, because "what does adding this person mean" is a property of the parent
  record it is adding them to.
- **``KeyProvisioningService`` owns the provisioning lifecycle** — every
  ``pending → minting → provisioned | failed`` transition, plus ``suspended`` on
  deactivation. It never creates or deletes a membership.
"""
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.core.security import encrypt_field
from app.models.credentials.ai_credential import (
    AICredential,
    AICredentialCreate,
    AICredentialData,
    AICredentialType,
    AICredentialUpdate,
)
from app.models.credentials.managed_ai_credential import (
    AdminAIKeyKind,
    AdminAIKeyRow,
    AdminAIKeysPublic,
    ManagedAICredential,
    ManagedAICredentialApplyCandidate,
    ManagedAICredentialApplyResult,
    ManagedAICredentialCreate,
    ManagedAICredentialMember,
    ManagedAICredentialPublic,
    ManagedAICredentialReconcileResult,
    ManagedAICredentialUpdate,
    ManagedReconcileBlock,
    ManagedReconcileSkip,
)
from app.models.credentials.managed_ai_credential import (
    ManagedDefaultSlotSkip,
)
from app.models.credentials.managed_ai_credential_membership import (
    ManagedAICredentialMembership,
    MembershipProvisioningStatus,
)
from app.models.users.user import AIKeyOnboardingState, User
from app.services.credentials.ai_credentials_service import (
    AICredentialInUseError,
    ai_credentials_service,
)
from app.services.credentials.provisioning_policy import (
    ProvisioningPolicy,
    resolve_policies,
    resolve_policy,
)
from app.services.credentials.key_provisioning_types import RevocationRequest
from app.services.environments.model_catalog import _strip_provider_prefix
from app.services.environments.sdk_constants import (
    is_credential_compatible_with_sdk,
)
from app.services.ai_providers import registry
from app.utils import restore_session

logger = logging.getLogger(__name__)


def _sdk_engine_for(cred_type: AICredentialType | str | None) -> str | None:
    """The ``<engine>/<provider>`` SDK string to compose for a credential type.

    One registry lookup. This used to be a five-entry table declared here and
    byte-identically in a second module, both mirroring the AddEnvironment SDK
    composition; the adapter is now the single declaration.
    """
    adapter = registry.find_adapter(cred_type)
    return adapter.sdk_engine if adapter is not None else None


class ManagedCredentialConflictError(Exception):
    """Two auto-provisioning configurations would fight over one default slot.

    **Neither raised nor caught in this module.** The rule moved with the thing
    it guards: auto-provision roles live on ``ai_provider``, so the collision is
    provider-vs-provider, ``AIProvidersService`` raises it as
    ``AIProviderConflictError``, and ``admin_ai_providers._conflict_409`` renders
    it. What is left here is the payload and the argument, kept as the base class
    that subclass is declared against — its only remaining consumer.

    ``default_ai_credential_<mode>_id`` holds exactly one credential. If two
    providers both auto-provision to the same role AND both wire that role's
    accounts' SDK default for the same mode, the account gets whichever one
    happened to be provisioned second — silently, differently per user if the row
    order ever changes, and invisibly to the admin who configured both. So the
    second configuration is refused at write time rather than resolved at grant
    time.

    Carries enough to name the other side in the 409 the route raises: an
    error that says only "conflict" leaves the admin to find the culprit among
    every credential on the page.
    """

    def __init__(
        self, *, conflicting_name: str, conflicting_id: uuid.UUID,
        role: str, mode: str,
    ) -> None:
        self.conflicting_name = conflicting_name
        self.conflicting_id = conflicting_id
        self.role = role
        self.mode = mode
        super().__init__(
            f"'{conflicting_name}' already sets the {mode} default for "
            f"auto-provisioned {role} accounts."
        )


@dataclass(frozen=True)
class MemberRow:
    """One member: the membership row, plus the child credential it names.

    The child is ``None`` whenever the membership's status says no key exists
    right now (``pending``, ``minting``, ``failed``, ``suspended``). Callers read
    ``membership.status`` for that fact and never re-derive it from ``child is
    None`` — the two can disagree while a reconcile is mid-flight, and the row is
    the one that is right.
    """

    membership: "ManagedAICredentialMembership"
    child: AICredential | None = None


@dataclass(frozen=True)
class MemberRemoval:
    """What removing one member did.

    At most one of the two is set: ``block`` when the row was refused and left
    intact, ``revocation`` when a key now needs destroying at the provider.
    Both ``None`` is the ordinary case of a member who held no provider key — a
    shared record's member, or a minted one whose mint never produced anything.
    """

    block: "ManagedReconcileBlock | None" = None
    revocation: "RevocationRequest | None" = None


@dataclass(frozen=True)
class ChildCreation:
    """One created child credential, plus the default slots it did not take.

    ``_add_child`` returns this rather than the bare row because the SDK-default
    wiring can decline a slot that somebody else already holds, and that fact
    has to travel to the reconcile result. Bundling it with the child is what
    keeps the two from being reported by different code paths that could
    disagree about which member they belong to.
    """

    child: AICredential
    slot_skips: list[ManagedDefaultSlotSkip] = field(default_factory=list)


@dataclass(frozen=True)
class MemberAddition:
    """What one add-only membership grant did.

    Deliberately not a ``ManagedAICredentialReconcileResult``: see
    :meth:`ManagedAICredentialsService.add_members`.
    """

    added: list[ManagedAICredentialMember] = field(default_factory=list)
    skipped: list[ManagedReconcileSkip] = field(default_factory=list)
    #: Default slots left alone because the owner already held one. Always empty
    #: when the caller passed ``claim_held_slots=True``.
    default_slot_skips: list[ManagedDefaultSlotSkip] = field(
        default_factory=list
    )


class ManagedAICredentialsService:
    """Superuser-only CRUD over the parent record + reconcile routine."""

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _get_parent_or_404(
        self, session: Session, managed_credential_id: uuid.UUID
    ) -> ManagedAICredential:
        parent = session.get(ManagedAICredential, managed_credential_id)
        if parent is None:
            raise HTTPException(
                status_code=404,
                detail="Managed AI credential not found",
            )
        return parent

    # ── Policy reads ─────────────────────────────────────────────────
    # ``provisioning_mode``, ``auto_provision_roles`` and every wiring flag are
    # read through ``provisioning_policy.resolve_policy``, never off the
    # record's own columns and never off ``AIProvider`` directly. This module
    # therefore holds no query against the provider table at all;
    # ``tests/architecture/ai_provider_isolation_test.py`` keeps it that way.

    def _decrypt_parent(
        self, session: Session, parent: ManagedAICredential
    ) -> AICredentialData:
        """Decrypt the key this record hands out, wherever it lives.

        Two places, one for each shape:

        * **Manual record** — the key is ``parent.encrypted_data``, as it always
          was.
        * **``fixed_key`` provider-owned record** — the provider is the only
          source of truth for the key, so it is read from
          ``AIProvider.encrypted_secret``. The bytes are in the same envelope:
          migration ``c23d6b59a8f5`` moved them across unchanged, and whatever
          writes one later has to keep doing so. Covered directly by
          ``tests/unit/test_managed_credential_key_source.py`` and exercised
          throughout ``tests/api/ai_credentials/ai_providers_service_test.py``,
          which builds ``fixed_key`` providers through
          ``AIProvidersService.create``.

        **Refuses a minted record**, which has no key anywhere: each member's is
        created at the provider. Every decrypt site in this service reaches here,
        so the guard is stated once rather than as an ``if parent.encrypted_data``
        at each of them, some of which would eventually be written as ``or ""``
        and hand an empty key to something that stores it.
        """
        from app.core.security import decrypt_field
        from app.services.credentials.ai_providers_service import (
            ai_providers_service,
        )

        if parent.provider_id is not None:
            # **Preferred, not a fallback**, and the difference is the bug it
            # closes. Reading ``encrypted_data`` first and only then asking the
            # provider means a stale key left on the record — one written before
            # the provider took ownership, or by a write that should have been
            # refused — silently outranks the provider's. A later
            # ``rotate_key`` would then re-key every member with the key it had
            # just replaced and report success.
            #
            # The provider service is the only module allowed to read the
            # provider table, so the envelope comes through it. It answers
            # ``None`` for a ``minted`` provider, whose secret is an
            # administration key and would become a member's model key if it
            # were handed back.
            blob = ai_providers_service.fixed_key_envelope(
                session, parent.provider_id
            )
        else:
            blob = parent.encrypted_data
        if not blob:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This managed AI credential mints a separate key for each "
                    "member and holds no key of its own."
                ),
            )
        data_dict = json.loads(decrypt_field(blob))
        return AICredentialData(**data_dict)

    def _encrypt_key(
        self,
        cred_type: AICredentialType,
        api_key: str,
        base_url: str | None,
        model: str | None,
    ) -> str:
        """Validate + Fernet-encrypt the canonical key into the parent shape.

        Per-type validation is reused from the per-user pipeline so the parent
        and its children share identical rules (e.g. openai_compatible requires
        base_url + model).
        """
        ai_credentials_service._validate_credential_data(
            cred_type, api_key, base_url, model
        )
        payload = AICredentialData(
            api_key=api_key, base_url=base_url, model=model
        )
        return encrypt_field(json.dumps(payload.model_dump()))

    def _current_members(
        self, session: Session, parent: ManagedAICredential
    ) -> dict[uuid.UUID, MemberRow]:
        """Map ``owner_id -> `` :class:`MemberRow` for this parent's members.

        **Two queries, whatever the membership size** — the memberships, then the
        children they name — because every caller wants both and the alternative
        is a ``session.get`` per member inside a loop that already runs per
        parent on the fleet-wide list.

        This is THE definition of membership. It reads
        ``managed_ai_credential_membership`` and nothing else; a child credential
        is a *consequence* of membership, no longer the evidence for it. Anything
        still deriving membership from ``AICredential.managed_credential_id``
        would be a second answer to a question that now has one.
        """
        memberships = session.exec(
            select(ManagedAICredentialMembership).where(
                ManagedAICredentialMembership.managed_credential_id == parent.id
            )
        ).all()
        child_ids = [m.ai_credential_id for m in memberships if m.ai_credential_id]
        children: dict[uuid.UUID, AICredential] = {}
        if child_ids:
            children = {
                row.id: row
                for row in session.exec(
                    select(AICredential).where(col(AICredential.id).in_(child_ids))
                ).all()
            }
        return {
            m.user_id: MemberRow(
                membership=m,
                child=children.get(m.ai_credential_id) if m.ai_credential_id else None,
            )
            for m in memberships
        }

    def _member_dto(
        self,
        member: MemberRow,
        owner: User | None,
        *,
        key_state: AIKeyOnboardingState,
    ) -> ManagedAICredentialMember:
        """Project one member row for the API.

        The status is read straight off the membership row. It is never inferred
        from whether ``child_credential_id`` came out null — that inference is
        exactly the client-side policy re-derivation this phase exists to
        prevent, and it would be wrong in both directions the moment a shared
        member's child is missing.

        ``key_state`` is passed in rather than computed here, and it is
        deliberately a required keyword: it is an account-wide question, the
        member list asks it for everybody at once (see :meth:`_owner_key_states`),
        and a default would let a caller silently ship a wrong answer.
        """
        membership = member.membership
        return ManagedAICredentialMember(
            user_id=membership.user_id,
            email=owner.email if owner else "",
            full_name=owner.full_name if owner else None,
            child_credential_id=membership.ai_credential_id,
            is_default=member.child.is_default if member.child else False,
            provisioning_status=MembershipProvisioningStatus(membership.status),
            provision_error=membership.last_error,
            provision_attempts=membership.provision_attempts,
            api_key_onboarding_state=key_state,
        )

    def _project_members(
        self, session: Session, rows: list[MemberRow]
    ) -> list[ManagedAICredentialMember]:
        """Project a batch of members in a **fixed** number of queries.

        The batched entry point for :meth:`_member_dto`, and the reason it
        exists is that ``_member_dto`` takes ``key_state`` as a required keyword
        — which correctly refuses a wrong default, and just as correctly does
        nothing to stop a caller satisfying it inside a per-member loop. Three
        of them did: ``add_members``' two branches and ``reconcile``'s update
        pass each called ``_owner_key_states(session, [owner_id])``, two queries
        apiece, so a PATCH adding two hundred members issued four hundred extra
        queries on a request path. ``_to_public`` was batched and the writers
        were not, which is the shape the predicate's own docstring warns about:
        an N+1 is how a cheap question becomes a reason not to ask it.

        It takes the owner lookup too, rather than a caller-supplied ``User``.
        Half a batching is how the other half comes back: a signature that
        accepts the owner leaves every caller free to fetch it per member, which
        is what ``reconcile``'s update pass did. ``_to_public`` resolves owners
        the same way and for the same reason.

        Call it **after** the writes it projects. The states it reads are the
        ones those writes just changed.
        """
        if not rows:
            return []
        owner_ids = {member.membership.user_id for member in rows}
        owners = {
            row.id: row
            for row in session.exec(
                select(User).where(col(User.id).in_(owner_ids))
            ).all()
        }
        # ``owners.get`` may miss, and ``_member_dto`` tolerates ``None`` for
        # the same reason it does in ``_to_public``: a user row that has gone
        # while its membership survived still has to project.
        key_states = self._owner_key_states(session, owner_ids)
        return [
            self._member_dto(
                member,
                owners.get(member.membership.user_id),
                key_state=key_states.get(
                    member.membership.user_id, AIKeyOnboardingState.NEEDS_KEY
                ),
            )
            for member in rows
        ]

    @staticmethod
    def _owner_key_states(
        session: Session, user_ids
    ) -> dict[uuid.UUID, AIKeyOnboardingState]:
        """Every named user's onboarding state, in a fixed number of queries.

        Imported locally because ``KeyProvisioningService`` imports this module —
        the same shape ``delete`` already uses for the same reason. The answer is
        that service's to give: this one must not grow a second opinion about
        whether somebody has a usable key.
        """
        from app.services.credentials.key_provisioning_service import (
            key_provisioning_service,
        )

        return key_provisioning_service.api_key_onboarding_states(
            session, list(user_ids)
        )

    @staticmethod
    def _dedup(ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """De-duplicate ids preserving order."""
        seen: set[uuid.UUID] = set()
        return [i for i in ids if not (i in seen or seen.add(i))]

    # Cap on curated list length / per-entry length (bound payload size).
    _AVAILABLE_MODELS_MAX = 100
    _MODEL_ID_MAX = 255

    @classmethod
    def _normalize_default_model(cls, value: str | None) -> str | None:
        """Normalize an admin ``default_model``: trim, strip any ``provider/``
        prefix, cap length. Blank → ``None``."""
        if value is None:
            return None
        cleaned = _strip_provider_prefix(value.strip())[: cls._MODEL_ID_MAX].strip()
        return cleaned or None

    @classmethod
    def _normalize_available_models(
        cls, value: list[str] | None
    ) -> list[str] | None:
        """Normalize an admin ``available_models`` list.

        Distinguishes ``None`` (no change / unset) from ``[]`` (explicit clear):
        ``None`` is returned as-is; a list is trimmed, ``provider/``-stripped,
        de-duplicated (order-preserving), emptied of blanks, and capped. An
        all-blank list normalizes to ``[]`` (still an explicit clear).
        """
        if value is None:
            return None
        seen: set[str] = set()
        out: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                continue
            entry = _strip_provider_prefix(raw.strip())[: cls._MODEL_ID_MAX].strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            out.append(entry)
            if len(out) >= cls._AVAILABLE_MODELS_MAX:
                break
        return out

    #: The fields a provider owns once it owns the record. Named here so the
    #: refusal below and the message it produces cannot drift apart; the same
    #: set is derived independently by
    #: ``tests/architecture/managed_credential_shadowed_fields_test.py`` from
    #: the plan's §3.2 list, and that test fails if the two disagree.
    SHADOWED_FIELDS = (
        "set_as_default",
        "set_user_sdk_defaults",
        "sdk_default_modes",
        "default_model",
        "available_models",
        "model_override_conversation",
        "model_override_building",
        "expiry_notification_date",
    )

    def _refuse_shadowed_writes(
        self,
        data: ManagedAICredentialUpdate,
        policy: ProvisioningPolicy,
    ) -> None:
        """400 on any attempt to edit a provider-owned record's wiring policy.

        Every submitted field is named, not just the first: an admin who sent
        three should not have to discover them one round trip at a time.

        **Three fields are refused here that are not in :data:`SHADOWED_FIELDS`**,
        and they are added to the local tuple rather than to that constant
        because it is the §3.2 set, pinned field-for-field by
        ``tests/architecture/managed_credential_shadowed_fields_test.py``.

        * ``api_key`` — the key itself. Accepting it would write
          ``encrypted_data`` on a record whose key the provider owns, and the
          next ``AIProvidersService.rotate_key`` would re-key every member from
          that stale copy while telling the admin the rotation succeeded: the
          "believing they rolled a key they did not" failure, reached through the
          credential surface instead of the provider one.
        * ``base_url`` and ``model`` — the key's *shape*, and for a ``fixed_key``
          provider they live in the provider's encrypted envelope, not here.
          ``_add_child`` builds each new member's credential straight out of that
          envelope (``_decrypt_parent`` prefers the provider), while an edit made
          on this record only reaches members who already exist. So accepting one
          wrote through to today's members and was silently reverted for every
          member added afterwards, and by the next ``rotate_key`` — two members
          of one credential holding different base URLs, with nothing reporting
          the divergence. The provider's own ``PATCH`` is the edit that
          re-encrypts the envelope, which is why it reaches both
          (``ai_providers_service_test.py::test_editing_a_providers_base_url_reaches_members_added_afterwards``).
        """
        offending = [
            name
            for name in (*self.SHADOWED_FIELDS, "api_key", "base_url", "model")
            if getattr(data, name, None) is not None
        ]
        if not offending:
            return
        provider = policy.provider_name or "its AI provider"
        raise HTTPException(
            status_code=400,
            detail=(
                f"{', '.join(offending)} {'is' if len(offending) == 1 else 'are'}"
                f" managed by the AI provider '{provider}'. Edit the provider "
                "instead; changing it there re-applies to every member."
            ),
        )

    # ``_claimed_slots`` and ``_validate_auto_provision_uniqueness`` moved to
    # ``AIProvidersService``. The rule they enforce — one owner per
    # ``(role, mode)`` default slot — is now **provider vs provider**, because
    # the rule that claims a slot lives on ``ai_provider`` and a managed
    # credential has no auto-provision roles of its own to fight with. The
    # claim itself is computed by ``ProvisioningPolicy.claimed_slots``, so the
    # write-time check and the run-time grant read the same definition.

    # ------------------------------------------------------------------ #
    # Per-child operations (delegate to ai_credentials_service)
    # ------------------------------------------------------------------ #

    def _stamp_child(
        self,
        session: Session,
        child: AICredential,
        parent: ManagedAICredential,
        policy: ProvisioningPolicy,
    ) -> None:
        """Stamp the admin-managed markers + structural parent link on a freshly
        created child so it looks exactly like today's admin-managed rows.

        Also writes through the admin-curated model metadata
        (``default_model`` / ``available_models``) directly on the child row —
        these are non-secret plain columns, so no ``update_credential`` round-trip
        is needed (they are not part of the encrypted ``AICredentialData``). The
        values come off the resolved policy, so a provider-owned record writes
        the *provider's* curation and a manual one writes its own."""
        child.is_admin_managed = True
        child.managed_by_id = parent.managed_by_id
        child.managed_credential_id = parent.id
        child.default_model = policy.default_model
        child.available_models = policy.available_models
        session.add(child)
        session.commit()
        session.refresh(child)

    def _apply_sdk_defaults(
        self,
        session: Session,
        owner: User,
        child: AICredential,
        policy: ProvisioningPolicy,
        *,
        claim_held_slots: bool,
    ) -> list[ManagedDefaultSlotSkip]:
        """Wire the owner's ``default_sdk_*`` + ``default_ai_credential_*_id`` for
        the policy's ``sdk_default_modes``. A mode whose composed engine is
        incompatible with the type is skipped (not a hard error).

        **The incumbent wins, unless the caller is a deliberate admin act.**
        ``claim_held_slots`` is keyword-only and has no default, because the two
        answers are the difference between a company key arriving quietly and a
        company key silently displacing the one a person chose:

        * ``False`` — automatic provisioning (a new account, an invite grant, a
          minted key materialising). A slot whose
          ``default_ai_credential_<mode>_id`` already names *any* credential is
          left exactly as it is, and the fact is returned as a
          :class:`ManagedDefaultSlotSkip`. The person still becomes a member and
          still gets the credential; only the default is not taken. This is a
          normal, expected situation — somebody pastes their own key and a
          provider later grants them one — and it is resolved rather than
          prevented.
        * ``True`` — ``apply_to_existing`` and ``set-default-all``, the two
          explicit "make this the default for everyone" actions, plus an admin
          naming members by hand. Those overwrite, which is what they are for,
          and produce no skips.

        The per-mode model override is written for every slot actually claimed,
        *unconditionally*, including back to ``NULL`` when the policy has no
        override of its own. That is a reset rather than a wipe because of when
        it runs: the credential pointer for this mode is moving onto a different
        credential than the one it named before, so whatever override was
        sitting in the slot described that other credential and may well name a
        model this provider does not serve. A slot that was *not* claimed is not
        touched at all — resetting the override of a default that stays where it
        was would be exactly the silent data loss the skip exists to avoid.

        The update path is the opposite case and behaves the opposite way —
        see :meth:`_sync_model_overrides`.
        """
        sdk_engine = _sdk_engine_for(child.type)
        if not sdk_engine:
            return []

        skips: list[ManagedDefaultSlotSkip] = []
        for mode in policy.sdk_default_modes:
            if mode not in self._MODE_POINTER_ATTR:
                continue
            if not is_credential_compatible_with_sdk(sdk_engine, child.type):
                continue
            incumbent = getattr(owner, self._MODE_POINTER_ATTR[mode])
            if incumbent is not None and not claim_held_slots:
                skips.append(
                    ManagedDefaultSlotSkip(
                        user_id=owner.id,
                        mode=mode,
                        held_by_credential_id=incumbent,
                    )
                )
                continue
            override = policy.model_override_for(mode)
            if mode == "conversation":
                owner.default_sdk_conversation = sdk_engine
                owner.default_ai_credential_conversation_id = child.id
                owner.default_model_override_conversation = override
            else:
                owner.default_sdk_building = sdk_engine
                owner.default_ai_credential_building_id = child.id
                owner.default_model_override_building = override

        session.add(owner)
        session.commit()
        session.refresh(owner)
        return skips

    # The two per-mode profile columns, keyed by mode, so the "conversation
    # else building" branch is not written out a fourth time. Membership of
    # these dicts is also the validity test for a mode string coming off a
    # JSON column.
    _MODE_POINTER_ATTR = {
        "conversation": "default_ai_credential_conversation_id",
        "building": "default_ai_credential_building_id",
    }
    _MODE_OVERRIDE_ATTR = {
        "conversation": "default_model_override_conversation",
        "building": "default_model_override_building",
    }

    def _sync_model_overrides(
        self,
        session: Session,
        policy: ProvisioningPolicy,
        child: AICredential,
        cleared_overrides: dict[str, str] | None = None,
    ) -> bool:
        """Push a changed model override onto an *existing* member's profile.

        Narrower than :meth:`_apply_sdk_defaults`, and the narrowings are the
        point. That one runs when the slot is **claimed** (a member is added,
        the credential pointer moves onto this child) and may therefore reset
        everything in the slot. This one runs on **every** update — a rename, a
        key rotation, an expiry-date edit — against a slot the child already
        occupies, so it must touch as little as possible:

        * **Only while the pointer still names this child.** A user who has
          since pointed their default at their own credential is not this
          record's business any more.
        * **Only when this record has an opinion.** A stored ``None`` means
          "no opinion about the model", not "clear theirs". Writing it through
          on every update would mean an admin renaming a record silently
          erased the model every member had chosen for it.

        ``cleared_overrides`` is the third case, and the one that keeps
        "no opinion" from swallowing an admin who *just said* they have none.
        It maps mode → the value this very request dropped (see
        :meth:`update`), and it is a **transition**, not a state: a stored
        ``None`` alone cannot distinguish "never set" from "just cleared",
        which is exactly why the caller has to say. For such a mode the
        member's pin is retracted — but only when it still equals the dropped
        value, i.e. only when it is the value this record wrote. A member who
        picked their own model against this credential keeps it; the admin
        retracted their own opinion, not the user's.

        Without that, clearing an override would be cosmetic: the record shows
        blank, every existing member stays pinned to the old id, and no admin
        action can ever remove it — a worse state than not offering the clear.

        The guard is narrower than "we respect the member's choice", and the
        asymmetry is deliberate rather than accidental: *setting* an override
        writes through unconditionally and does overwrite a model the member
        picked (asserted as intended in
        ``test_claiming_a_slot_resets_the_override_but_holding_one_does_not_wipe_it``),
        while *clearing* one only retracts this record's own value. So the
        admin's opinion outranks the member's while it exists and defers to it
        once withdrawn — which also means a member's own pick, once overwritten
        by a set, is not restored by the later clear.

        Returns True iff the owner row was written.
        """
        if not policy.set_user_sdk_defaults:
            return False
        owner = session.get(User, child.owner_id)
        if owner is None:
            return False

        dropped = cleared_overrides or {}
        dirty = False
        # A mode dropped from ``sdk_default_modes`` is not visited, so neither
        # its override nor its credential pointer is torn down. That matches
        # how the pointer has always behaved — dropping a mode stops this
        # record managing it, it does not un-manage what it already set — and
        # the pair is left consistent on purpose rather than by omission.
        for mode in policy.sdk_default_modes:
            if mode not in self._MODE_OVERRIDE_ATTR:
                continue
            if getattr(owner, self._MODE_POINTER_ATTR[mode]) != child.id:
                continue
            attr = self._MODE_OVERRIDE_ATTR[mode]
            override = policy.model_override_for(mode)
            if override is None:
                retracted = dropped.get(mode)
                if retracted is None or getattr(owner, attr) != retracted:
                    continue
                setattr(owner, attr, None)
                dirty = True
                continue
            if getattr(owner, attr) != override:
                setattr(owner, attr, override)
                dirty = True

        if dirty:
            session.add(owner)
            session.commit()
            session.refresh(owner)
        return dirty

    def _add_child(
        self,
        session: Session,
        parent: ManagedAICredential,
        policy: ProvisioningPolicy,
        owner: User,
        key: AICredentialData,
        *,
        claim_held_slots: bool,
    ) -> "ChildCreation":
        """Create one child for ``owner`` via the per-user pipeline, then stamp
        it + apply optional default / SDK-default wiring.

        Returns a :class:`ChildCreation` rather than the bare child, because the
        SDK-default wiring can now decline a slot and that fact has to reach the
        reconcile result. ``claim_held_slots`` is passed straight through to
        :meth:`_apply_sdk_defaults`; see there for what the two answers mean.

        The child is fully created + stamped (committed = a real member) BEFORE
        the optional default/SDK wiring runs. If that post-create wiring throws,
        we log and still return the committed child rather than letting the
        caller report the user in ``skipped`` — the row exists and IS a member
        (membership is derived from ``managed_credential_id``), so reporting it
        as failed would be the inverse of a phantom member. Only a failure
        BEFORE the child is committed (create/stamp) propagates → ``skipped``.

        That promise holds for *every* failure class, which took a second pass
        to be true: the handler repairs the session and logs only snapshotted
        identifiers, because on an aborted transaction the log line's own
        arguments were lazy loads and the exception escaped through it.
        """
        public = ai_credentials_service.create_credential(
            session,
            owner.id,
            AICredentialCreate(
                name=parent.name,
                type=parent.type,
                api_key=key.api_key,
                base_url=key.base_url,
                model=key.model,
                expiry_notification_date=policy.expiry_notification_date,
            ),
        )
        child = session.get(AICredential, public.id)
        self._stamp_child(session, child, parent, policy)

        # Snapshotted while the session is known good. ``_stamp_child`` has
        # just committed, which expires the identity map, so every one of
        # these is a lazy load from here on — and a lazy load is a query the
        # handler below cannot afford to make.
        child_id = child.id
        owner_id = owner.id
        parent_id = parent.id

        # --- Post-commit wiring: best-effort, never demotes a created member. ---
        slot_skips: list[ManagedDefaultSlotSkip] = []
        try:
            if policy.set_as_default:
                ai_credentials_service.set_default(session, child.id, owner.id)
                session.refresh(child)
            if policy.set_user_sdk_defaults:
                slot_skips = self._apply_sdk_defaults(
                    session,
                    owner,
                    child,
                    policy,
                    claim_held_slots=claim_held_slots,
                )
        except Exception:
            # Repair, then log. Without the rollback this handler only keeps
            # the promise in the docstring for *Python*-level failures: a
            # statement-level one leaves the transaction aborted, the log line
            # above used to be three lazy loads, and the exception escaped into
            # ``add_members`` — which reported the user ``provision_failed``
            # while their child row sat committed and was, by every definition
            # this service uses, a member. On the auto-provision path that
            # wrote an ``auto_provision_failed`` event into the security feed
            # of someone who had in fact received the credential.
            #
            # Rolling back here loses only the failed wiring: the child was
            # committed by ``_stamp_child``, and ``set_default`` /
            # ``_apply_sdk_defaults`` each commit their own work.
            restore_session(session)
            logger.exception(
                "Child %s created for user %s under parent %s but post-create "
                "default/SDK wiring failed; member retained.",
                child_id, owner_id, parent_id,
            )

        return ChildCreation(child=child, slot_skips=slot_skips)

    def materialise_minted_child(
        self,
        session: Session,
        parent: ManagedAICredential,
        owner: User,
        api_key: str,
    ) -> AICredential:
        """Create the child credential for a member whose key has just been minted.

        Goes through exactly the same ``_add_child`` as a shared member, with the
        key coming from the provider instead of from the parent row. That is the
        point: "how does a member's credential get created, stamped and wired
        into their defaults" has one implementation, so a minted member cannot
        quietly end up with different defaults, a different admin-managed marker
        or a different SDK wiring than a shared one.

        The three model-level guards that refuse an empty key are left exactly as
        they are, and this path does not need relaxing them: it is called only
        once a real key exists.

        **The default-slot intent is read off the membership row**, not decided
        here. This runs on a converge pass with no idea whether a superuser
        pressed "apply to existing users" or an account simply signed up, and
        both answers are wrong for half the callers: always claiming lets
        automatic provisioning steal a default (§5.5 says it never may), never
        claiming silently breaks the two deliberate escape hatches for exactly
        the provider kind — per-user minted keys — the feature's headline
        scenario uses. The grant recorded which it was; this reads it back.

        The slot skips it can produce are dropped: there is no reconcile result
        to carry them, the request that created the membership returned long
        ago, and ``ConvergeReport`` has no field for them. That is a real gap in
        what an administrator can see, not a tidy-up — a minted member who kept
        their own default is currently indistinguishable from one who never had
        one.
        """
        key = AICredentialData(
            api_key=api_key, base_url=parent.base_url, model=parent.model
        )
        membership = session.exec(
            select(ManagedAICredentialMembership).where(
                ManagedAICredentialMembership.managed_credential_id == parent.id,
                ManagedAICredentialMembership.user_id == owner.id,
            )
        ).first()
        return self._add_child(
            session,
            parent,
            resolve_policy(session, parent),
            owner,
            key,
            # No membership row is not a state this path can reach — the mint it
            # is the tail of was claimed off one — but the incumbent-wins
            # default is the safe answer if it ever did.
            claim_held_slots=bool(
                membership is not None and membership.claim_held_default_slots
            ),
        ).child

    def _update_child_fields(
        self,
        session: Session,
        parent: ManagedAICredential,
        policy: ProvisioningPolicy,
        child: AICredential,
        *,
        key_rotated: bool,
        key: AICredentialData | None,
        cleared_overrides: dict[str, str] | None = None,
    ) -> bool:
        """Write changed scalar fields (and rotated key) through to a child via
        the per-user pipeline, then apply/clear default per ``set_as_default``.

        The record's own columns supply the child's *shape* (name, base_url,
        model); the resolved policy supplies everything the provider owns
        (curated models, expiry, the default flags, the per-mode overrides). A
        provider edit therefore reaches every existing member through exactly
        this method — that is what makes §5.3's "a policy edit re-applies"
        true, rather than a second write path that has to be kept in step.

        Diffs parent-vs-child first and only writes when something actually
        changed, so a no-op reconcile is genuinely a no-op (idempotency).
        Returns ``True`` iff this child was mutated (so the caller can count it
        and emit an update event).

        Clear-through limitation: ``ai_credentials_service.update_credential``
        treats a ``None`` field as "leave unchanged", so it cannot express
        clearing ``base_url`` / ``model`` / ``expiry_notification_date`` back to
        ``None``. ``expiry_notification_date`` is therefore cleared directly on
        the child row here (it has no per-type validation coupling). ``base_url``
        / ``model`` are NOT cleared-through: for the only type that uses them
        (``openai_compatible``) both are required, so clearing them would fail
        validation anyway — a non-None replacement is the only valid edit.
        """
        existing = ai_credentials_service.decrypt_credential(child)

        # Diff non-secret scalars + key rotation. ``None`` parent values for
        # base_url/model are treated as "no change" (cannot clear-through; see
        # docstring) so they don't spuriously flag a diff.
        name_changed = parent.name != child.name
        base_url_changed = (
            parent.base_url is not None and parent.base_url != existing.base_url
        )
        model_changed = (
            parent.model is not None and parent.model != existing.model
        )
        expiry_changed = (
            policy.expiry_notification_date != child.expiry_notification_date
        )
        fields_changed = (
            name_changed or base_url_changed or model_changed or key_rotated
        )

        changed = False

        if fields_changed:
            update = AICredentialUpdate(
                name=parent.name if name_changed else None,
                base_url=parent.base_url if base_url_changed else None,
                model=parent.model if model_changed else None,
                # expiry handled separately below (update_credential can't clear
                # to None); only pass through a non-None set value here.
                expiry_notification_date=(
                    policy.expiry_notification_date
                    if (
                        expiry_changed
                        and policy.expiry_notification_date is not None
                    )
                    else None
                ),
                api_key=key.api_key if (key_rotated and key) else None,
            )
            ai_credentials_service.update_credential(
                session, child.id, child.owner_id, update, admin_override=True
            )
            session.refresh(child)
            changed = True

        # Clear-through for expiry → None (update_credential can't express it).
        if expiry_changed and policy.expiry_notification_date is None:
            child.expiry_notification_date = None
            child.updated_at = datetime.now(timezone.utc)
            session.add(child)
            session.commit()
            session.refresh(child)
            changed = True

        # Admin-curated model metadata write-through. These are non-secret plain
        # columns (not part of AICredentialData), so we write them DIRECTLY on the
        # child row — bypassing update_credential entirely (parallel to the expiry
        # clear-through above). The parent values are already normalized at store
        # time. Idempotent: only write (and only flag changed) on an actual diff.
        # ``available_models`` distinguishes None (no change) from [] (clear) by
        # comparing exact stored values: the parent itself carries None vs [].
        curated_changed = False
        if policy.default_model != child.default_model:
            child.default_model = policy.default_model
            curated_changed = True
        if policy.available_models != child.available_models:
            child.available_models = policy.available_models
            curated_changed = True
        if curated_changed:
            child.updated_at = datetime.now(timezone.utc)
            session.add(child)
            session.commit()
            session.refresh(child)
            changed = True

        # Per-mode model override write-through for slots this child still
        # occupies (see ``_sync_model_overrides``).
        if self._sync_model_overrides(
            session, policy, child, cleared_overrides
        ):
            changed = True

        # Default flag application/clear (counts as a change of its own).
        if policy.set_as_default and not child.is_default:
            ai_credentials_service.set_default(session, child.id, child.owner_id)
            session.refresh(child)
            changed = True
        elif not policy.set_as_default and child.is_default:
            self._clear_child_default(session, child)
            changed = True

        return changed

    def _clear_child_default(
        self, session: Session, child: AICredential
    ) -> None:
        """Clear the default flag on a child + un-wire it from the owner's
        profile.

        Un-wires both:
        - the legacy ``ai_credentials_encrypted`` profile blob for the type
          (mirror of ``set_default``'s profile sync), and
        - the owner's ``default_ai_credential_conversation_id`` /
          ``default_ai_credential_building_id`` (and their ``default_sdk_*``)
          when they point at THIS child — because this service is what set them
          via ``_apply_sdk_defaults``, so it must also tear them down. (Plain
          ``set_default`` does not touch these; here we own that wiring.)
        """
        owner = session.get(User, child.owner_id)
        cred_type = child.type
        child.is_default = False
        child.updated_at = datetime.now(timezone.utc)
        session.add(child)
        session.commit()
        session.refresh(child)
        if owner is None:
            return

        ai_credentials_service._clear_user_profile_for_type(
            session, owner, cred_type
        )

        # Un-wire SDK-default pointers that reference this child.
        # The model override is torn down with the pointer it belongs to:
        # ``_apply_sdk_defaults`` set the two together, so leaving the override
        # behind would pin the user's *next* default credential to a model
        # chosen for the one just removed.
        dirty = False
        for mode in self._modes_pointing_at(owner, child.id):
            if mode == "conversation":
                owner.default_ai_credential_conversation_id = None
                owner.default_sdk_conversation = None
                owner.default_model_override_conversation = None
            else:
                owner.default_ai_credential_building_id = None
                owner.default_sdk_building = None
                owner.default_model_override_building = None
            dirty = True
        if dirty:
            session.add(owner)
            session.commit()
            session.refresh(owner)

    @classmethod
    def _modes_pointing_at(cls, owner: User, child_id: uuid.UUID) -> list[str]:
        """The SDK-default modes whose credential pointer names ``child_id``.

        The one place the "does this slot belong to this child" question is
        answered, because two callers ask it at opposite ends of a child's
        life: :meth:`_clear_child_default` before it un-wires, and
        :meth:`_release_model_overrides` before the row is deleted out from
        under the pointer.
        """
        return [
            mode
            for mode, attr in cls._MODE_POINTER_ATTR.items()
            if getattr(owner, attr) == child_id
        ]

    def _release_model_overrides(
        self, session: Session, owner_id: uuid.UUID, modes: list[str]
    ) -> None:
        """Clear ``default_model_override_<mode>`` after a child is deleted.

        Deleting the child clears the owner's ``default_ai_credential_<mode>_id``
        — but only because that column carries ``ondelete="SET NULL"``, which
        is a database fact and knows nothing about the *override* sitting
        beside it. So a removed member kept a model id chosen for a credential
        they no longer hold, and it was then applied to whatever they selected
        next for that mode, possibly from a different provider entirely.
        ``_clear_child_default`` already ties the two together and says why;
        this is the same rule on the path that does not go through it —
        reconcile's Remove pass, and therefore ``DELETE
        /admin/llm-providers/{id}`` as well.

        ``modes`` must be captured *before* the delete: afterwards the pointer
        is already NULL and there is no way left to tell which slots this
        child owned.

        Called only after the delete has succeeded. A member whose removal was
        blocked (a child in use) stays a member, and their wiring must stay
        intact with them. One consequence worth stating: if a single PATCH both
        retracts an override and fails to remove a blocked member, that member
        keeps the retracted pin and no later request can express the
        retraction again — the transition is gone. Narrow enough to accept; the
        alternative is unpinning someone who did not actually leave.

        ``default_sdk_<mode>`` is deliberately left alone here, and NOT because
        it is harmless. Nothing re-derives it: ``EnvironmentService`` and
        ``ExternalAccountConfigService`` both read ``user.default_sdk_<mode>``
        as-is, so a removed member's next environment is still composed on the
        engine of a credential they no longer hold. It is left because it
        predates this phase, the same dangle exists on paths this service does
        not own, and a fix wants its own tests — not because it does nothing.
        ``_clear_child_default`` clears pointer + engine + override for the
        same slots, so the two teardown paths knowingly disagree about one
        field until then.
        """
        if not modes:
            return
        owner = session.get(User, owner_id)
        if owner is None:
            return
        dirty = False
        if (
            "conversation" in modes
            and owner.default_model_override_conversation is not None
        ):
            owner.default_model_override_conversation = None
            dirty = True
        if (
            "building" in modes
            and owner.default_model_override_building is not None
        ):
            owner.default_model_override_building = None
            dirty = True
        if dirty:
            session.add(owner)
            session.commit()
            session.refresh(owner)

    # ------------------------------------------------------------------ #
    # Membership — the Add pass, on its own
    # ------------------------------------------------------------------ #

    def add_members(
        self,
        session: Session,
        *,
        parent: ManagedAICredential,
        user_ids: list[uuid.UUID],
        actor: User | None,
        claim_held_slots: bool = False,
    ) -> MemberAddition:
        """Grant ``parent`` to ``user_ids``. Adds only — never removes.

        This is the Add pass of :meth:`reconcile`, lifted out because three
        callers need exactly it and nothing else: ``reconcile`` itself,
        ``AccountProvisioningService`` when an account is created, and
        "apply to existing users". Copying it would have been the third place
        the "validate active → decrypt → create child → wire defaults"
        sequence lives, and the one that quietly falls behind.

        Idempotent: ids that are already members are not re-added and are not
        reported — being a member is the outcome the caller asked for. The one
        exception is a **repair**, not an add: a shared member whose child
        credential has gone missing gets a new one, because a membership row with
        no key is the state this method exists to prevent. Minted members are
        never repaired here; their key is the provisioning pass's business.

        **What "added" means depends on the parent's mode**, and that is the
        whole point of the mode. For a shared record, a member is added *and*
        holds a key by the time this returns. For a minted record, a member is
        added with status ``pending`` and holds nothing yet — the key is created
        by ``KeyProvisioningService``, out of band. Callers that assume
        ``added`` implies a usable credential must read ``provisioning_status``
        instead; ``child_credential_id`` is ``None`` in the minted case.

        Returns :class:`MemberAddition`, not a
        ``ManagedAICredentialReconcileResult``. The reconcile shape carries
        ``removed`` / ``blocked`` / ``updated``, which an add-only operation
        can never populate, and a ``record`` projection that every one of the
        three callers throws away — ``reconcile`` and ``apply_to_existing``
        build their own, and the provisioning path reads only ``added``.
        Building it here would have put ``_to_public``'s per-member user
        lookup and its parent-key decrypt on the signup request path, for a
        value nobody reads.

        ``actor`` is the superuser who initiated this, or ``None`` for a
        system-initiated grant (account creation). It is keyword-only and has
        no default because "who did this" must be a decision at every call
        site, not an omission: a route that reached ``actor=None`` would be
        writing an unattributed grant, which is precisely what the audit trail
        exists to prevent. It does not affect what is written to the child —
        children are stamped with the *parent's* managing admin either way.

        ``claim_held_slots`` says whether this grant may take a default slot
        somebody already holds. It **defaults to False — the incumbent wins**,
        because the callers that do not pass it are the automatic ones
        (``AccountProvisioningService`` at account creation and at invite), and
        automatic provisioning never steals a default. Every deliberate admin
        act passes True: ``apply_to_existing``, ``set-default-all``, and
        ``reconcile`` — the last because a manual record's explicit member list
        has always claimed the slot and §3.2 of the ai-credential-providers plan
        keeps every control a manual record has today. Declined slots come back
        in ``default_slot_skips``; the member is added either way.

        **Returns on a healthy session, always.** A per-owner failure can be a
        statement-level one — a lock timeout, a serialization failure, a
        constraint the child insert trips — and Postgres leaves the whole
        transaction aborted after those, not just the statement. Recording a
        skip and carrying on without repairing that is a normal-looking return
        whose session detonates in the *next* thing the caller does: this
        method's own next iteration, ``reconcile``'s Remove pass,
        ``apply_to_existing``'s ``_to_public`` projection, the account-creation
        caller's confirmation-email commit. So the handler rolls back before it
        returns, and the guarantee belongs here rather than to whatever runs
        afterwards. ``AccountProvisioningService`` used to be the thing that
        made this path survive, by way of its audit write failing and repairing
        the session as a side effect — a guarantee that would have evaporated
        the day someone batched those events or made them lazy.

        Identifiers are snapshotted before the loop for the same reason the
        rollback is inside the handler: on an aborted transaction ``parent.id``
        is a query, so a handler that interpolates ORM attributes into its log
        line throws from inside the ``except`` and the exception escapes the
        net that was written to catch it.
        """
        desired = self._dedup(user_ids)
        # Snapshotted while the session is known good — see the docstring.
        parent_id = parent.id
        actor_id = actor.id if actor else None
        policy = resolve_policy(session, parent)
        minted = policy.is_minted
        current = self._current_members(session, parent)

        # Projected *after* the loop, in one batched key-state lookup — see
        # :meth:`_project_members`. Not before it either: adding a minted member
        # writes the pending membership that moves them from ``needs_key`` to
        # ``preparing``, so a state read ahead of the writes would ship the
        # answer the change was about to invalidate.
        added_rows: list[MemberRow] = []
        skipped: list[ManagedReconcileSkip] = []
        slot_skips: list[ManagedDefaultSlotSkip] = []
        key: AICredentialData | None = None

        for owner_id in desired:
            existing = current.get(owner_id)
            if existing is not None:
                # Already a member — the outcome the caller asked for. One
                # exception, and it is a repair rather than an add: a shared
                # member whose child row went missing (an out-of-band delete, a
                # create that committed the membership and then failed) has no
                # key, and leaving them a keyless member forever is the failure
                # this branch exists to prevent. A *minted* member is never
                # repaired here — their key is the provisioning pass's business,
                # and re-adding them would restart a state machine mid-flight.
                if minted or existing.child is not None:
                    continue
                if existing.membership.status != (
                    MembershipProvisioningStatus.NOT_APPLICABLE.value
                ):
                    continue

            owner = session.get(User, owner_id)
            if owner is None:
                skipped.append(
                    ManagedReconcileSkip(
                        user_id=owner_id, reason="user_not_found"
                    )
                )
                continue
            if not owner.is_active:
                skipped.append(
                    ManagedReconcileSkip(
                        user_id=owner_id, reason="user_inactive"
                    )
                )
                continue

            if minted:
                # No provider call here, ever. This runs inline on the signup
                # and OAuth-callback request paths (see
                # ``AccountProvisioningService``), where a provider timeout would
                # become a failed login. All that happens is that the intent is
                # recorded; the converge pass mints against it.
                try:
                    membership = self._upsert_membership(
                        session,
                        parent_id=parent_id,
                        user_id=owner_id,
                        status=MembershipProvisioningStatus.PENDING,
                        claim_held_slots=claim_held_slots,
                        existing=existing.membership if existing else None,
                    )
                except Exception:
                    restore_session(session)
                    logger.exception(
                        "Failed to record minted membership for user %s under "
                        "parent %s (actor=%s)",
                        owner_id, parent_id, actor_id or "system",
                    )
                    skipped.append(
                        ManagedReconcileSkip(
                            user_id=owner_id, reason="provision_failed"
                        )
                    )
                    continue
                added_rows.append(MemberRow(membership=membership))
                continue

            if key is None:
                key = self._decrypt_parent(session, parent)
            child = None
            try:
                creation = self._add_child(
                    session,
                    parent,
                    policy,
                    owner,
                    key,
                    claim_held_slots=claim_held_slots,
                )
                child = creation.child
                slot_skips.extend(creation.slot_skips)
                membership = self._upsert_membership(
                    session,
                    parent_id=parent_id,
                    user_id=owner_id,
                    status=MembershipProvisioningStatus.NOT_APPLICABLE,
                    claim_held_slots=claim_held_slots,
                    existing=existing.membership if existing else None,
                    ai_credential_id=child.id,
                )
            except HTTPException:
                # Type-validation errors etc. would have failed before
                # reconcile; re-raise so they are not silently swallowed.
                raise
            except Exception:
                # Repair first, log second. Both orderings look identical
                # against a Python-level exception; against an aborted
                # transaction the logging call is itself a query and the
                # "defensive" handler becomes the thing that raises.
                restore_session(session)
                logger.exception(
                    "Failed to provision child for user %s under parent %s "
                    "(actor=%s)",
                    owner_id, parent_id, actor_id or "system",
                )
                # A child that committed before the membership row failed is a
                # real credential nothing can now see: not a member (so no
                # reconcile will ever touch it), but usable by its owner. Undo it
                # so "skipped" means what it says. Best-effort — if the cleanup
                # also fails the row degrades to a plain admin-managed orphan,
                # which is the documented fallback for a parentless child.
                if child is not None:
                    self._discard_orphan_child(session, child.id, owner_id)
                skipped.append(
                    ManagedReconcileSkip(
                        user_id=owner_id, reason="provision_failed"
                    )
                )
                # The decrypted key is a plain value, not session state, so it
                # survives the rollback and the remaining owners are still
                # attempted.
                continue
            added_rows.append(MemberRow(membership=membership, child=child))

        return MemberAddition(
            added=self._project_members(session, added_rows),
            skipped=skipped,
            default_slot_skips=slot_skips,
        )

    def _validate_provisioning_shape(
        self, session: Session, data: ManagedAICredentialCreate
    ) -> None:
        """Refuse a manual record that has nothing to give anyone.

        Stated **server-side**, once, and not in the dialog that happens to be
        the only current caller: "may this record be created" is a policy, and a
        policy answered only in a browser is answered nowhere.

        One rule is left, and the shrinkage is the point. This route creates
        **manual** records only — a provider-owned one is created by creating
        its provider — so the five other incoherent shapes it used to refuse
        (shared-with-a-provider, a second credential on one provider, minted
        with a key, minted for a type that cannot mint, minted with nothing to
        mint through) are no longer expressible: every one of them needed
        ``provisioning_mode`` or ``provider_admin_credential_id``, and
        :class:`ManagedAICredentialCreate` carries neither and forbids unknown
        keys. What remains is the rule that was never about a provider — a
        manual record is always ``shared``, and a shared record must hold a key.

        ``session`` is kept in the signature although nothing reads it: the
        checks that queried are the ones that moved to the provider surface, and
        a caller-visible signature change buys nothing here.
        """
        if not data.api_key:
            raise HTTPException(
                status_code=400,
                detail="An API key is required for a shared credential.",
            )

    def _remove_one_member(
        self,
        session: Session,
        *,
        member: MemberRow,
        parent_id: uuid.UUID,
        parent_type: str,
        parent_provider_id: uuid.UUID | None,
        force: bool,
    ) -> "MemberRemoval":
        """Take one person's grant away: child first, then the key.

        The whole of what "removing a member" means, in one place, because there
        are now two callers who must not disagree about it — :meth:`reconcile`,
        where a removal is the difference between a desired set and the current
        one, and the keys list's per-key revoke, where it is the whole request.
        The second caller is the reason this is a method: a second
        implementation of this sequence would be a second opinion about whether
        the key is destroyed before or after the row that names it.

        Returns rather than raises. Every failure here is *per member* — the
        set-based caller records it and carries on with the rest — so the
        outcome is a value the caller decides what to do with.

        **It does not schedule the revocation it produces.** A revoke destroys a
        key at the provider and must happen only once the database has accepted
        every row change; the caller collects the requests and hands them off
        together at the end. Returning the request rather than firing it is what
        keeps that ordering the caller's to enforce.
        """
        membership = member.membership
        owner_id = membership.user_id
        child = member.child
        if membership.status == MembershipProvisioningStatus.MINTING.value:
            # A converge pass has claimed this row and is inside a provider
            # call right now, in another session. Deleting the row here means
            # its ``external_key_ref`` — which that call is about to write —
            # lands nowhere: the service account exists at the provider, no
            # row names it, and no revocation can ever be scheduled for it.
            # So the removal is refused for as long as the mint is in flight,
            # which is at most one converge tick. The admin sees it in
            # ``blocked`` and retries; that is a far better outcome than a key
            # nobody can find.
            return MemberRemoval(
                block=ManagedReconcileBlock.of(
                    user_id=owner_id, reason="mint_in_flight"
                )
            )
        # Read while the rows still exist, for two reasons that both make
        # the answer unrecoverable afterwards: the owner's default pointers
        # are NULLed by ``ondelete="SET NULL"``, and the membership row
        # (which carries the provider handles needed to revoke a minted key)
        # is about to be deleted.
        child_id = child.id if child else None
        revoke_ref = membership.external_key_ref
        owner = session.get(User, owner_id)
        held_modes = (
            self._modes_pointing_at(owner, child_id)
            if owner and child_id
            else []
        )
        try:
            if child is not None:
                ai_credentials_service.delete_credential(
                    session,
                    child.id,
                    child.owner_id,
                    force=force,
                    admin_override=True,
                )
        except AICredentialInUseError as in_use:
            return MemberRemoval(
                block=ManagedReconcileBlock.of(
                    user_id=owner_id,
                    reason="in_use_bundle",
                    impact=in_use.impact.model_dump(mode="json"),
                )
            )
        except HTTPException:
            raise
        except Exception:
            # Repair before logging, and before returning — the same rule
            # ``add_members`` states at length. A statement-level failure here
            # leaves the transaction aborted, and the set-based caller ends at
            # ``_to_public``, which queries: recording a block and carrying on
            # would turn a per-member problem into a 500 for the whole PATCH.
            # Nothing of the caller's is pending — ``delete_credential`` and
            # ``_release_model_overrides`` commit their own work, as does the
            # Add pass.
            restore_session(session)
            logger.exception(
                "Failed to remove child for user %s under parent %s",
                owner_id, parent_id,
            )
            return MemberRemoval(
                block=ManagedReconcileBlock.of(
                    user_id=owner_id, reason="remove_failed"
                )
            )
        # Only on the success path: a blocked member keeps their child and
        # must keep their wiring with it.
        self._release_model_overrides(session, owner_id, held_modes)
        self._delete_membership(session, membership)
        # **Delete first, revoke second**, and this is the reason: the delete
        # above can be refused by the Tier-2 blast-radius gate, and a revoke
        # that ran first would leave a dead key on a surviving row that reads
        # as healthy everywhere and fails at first use. A blocked member keeps
        # a working key instead, which is recoverable.
        revocation = None
        if revoke_ref:
            revocation = RevocationRequest(
                user_id=owner_id,
                parent_id=parent_id,
                provider_admin_credential_id=parent_provider_id,
                provider_type=parent_type,
                external_key_ref=revoke_ref,
                # The holder's own feed: they still exist, and "your
                # administrator removed you from this credential and the key
                # was destroyed" is exactly the entry that belongs there.
                audit_user_id=owner_id,
            )
        return MemberRemoval(revocation=revocation)


    def _delete_membership(
        self, session: Session, membership: ManagedAICredentialMembership
    ) -> None:
        """Remove one membership row. The person stops being a member here.

        Deleting the row is what "no longer a member" means — there is no
        tombstone status for it, because absence of a row already says exactly
        that and a second encoding of the same fact is a second answer. The
        durable states (``failed``, ``suspended``) describe members, not
        ex-members.
        """
        session.delete(membership)
        session.commit()

    def _discard_orphan_child(
        self, session: Session, child_id: uuid.UUID, owner_id: uuid.UUID
    ) -> None:
        """Best-effort removal of a child whose membership row never landed."""
        try:
            ai_credentials_service.delete_credential(
                session, child_id, owner_id, force=True, admin_override=True
            )
        except Exception:  # pragma: no cover - defensive
            restore_session(session)
            logger.exception(
                "Orphan child credential %s (owner %s) could not be removed "
                "after its membership row failed to write.",
                child_id, owner_id,
            )

    def _upsert_membership(
        self,
        session: Session,
        *,
        parent_id: uuid.UUID,
        user_id: uuid.UUID,
        status: MembershipProvisioningStatus,
        claim_held_slots: bool,
        existing: ManagedAICredentialMembership | None = None,
        ai_credential_id: uuid.UUID | None = None,
    ) -> ManagedAICredentialMembership:
        """Create (or repair) one membership row and commit it.

        Committed here rather than left pending because ``_add_child`` has
        already committed the child: a membership that only exists in the
        caller's session would be rolled back by the next per-owner failure and
        leave a real, invisible member behind.

        ``claim_held_slots`` is keyword-only with no default because it is the
        grant's *intent*, and for a minted member nothing else will remember it:
        the key — and therefore the default wiring — arrives on a converge pass
        long after this request has returned. See
        ``ManagedAICredentialMembership.claim_held_default_slots``.
        """
        now = datetime.now(timezone.utc)
        membership = existing or ManagedAICredentialMembership(
            managed_credential_id=parent_id,
            user_id=user_id,
            status=status.value,
            created_at=now,
        )
        membership.status = status.value
        membership.ai_credential_id = ai_credential_id
        membership.claim_held_default_slots = claim_held_slots
        membership.updated_at = now
        session.add(membership)
        session.commit()
        session.refresh(membership)
        return membership

    # ------------------------------------------------------------------ #
    # Reconcile — the heart
    # ------------------------------------------------------------------ #

    def reconcile(
        self,
        session: Session,
        admin: User,
        parent: ManagedAICredential,
        desired_user_ids: list[uuid.UUID],
        *,
        apply_fields: bool = True,
        force: bool = False,
        key_rotated: bool = False,
        cleared_overrides: dict[str, str] | None = None,
    ) -> ManagedAICredentialReconcileResult:
        """Diff desired-vs-actual membership and converge.

        - **Add** (desired − current): validate user exists/active (else
          ``skipped``); decrypt parent key; create child via the per-user
          pipeline; stamp markers + parent link; optional default / SDK defaults.
        - **Remove** (current − desired): delete child via the per-user pipeline
          (``admin_override=True``), then release the owner's per-mode model
          override for the slots that child held (the credential pointer itself
          is cleared by ``ondelete="SET NULL"``; the override beside it is not
          — see ``_release_model_overrides``). On ``AICredentialInUseError``
          append to ``blocked`` (member stays, wiring untouched) unless
          ``force``.
        - **Update** (current ∩ desired, when ``apply_fields``): write parent
          scalar fields (and the rotated key when ``key_rotated``) through to the
          child; apply/clear default per ``set_as_default``.

        Per-child failures are collected into ``skipped``/``blocked``; the
        successful children are committed. Idempotent: identical desired set +
        unchanged fields → empty added/removed/updated.

        ``cleared_overrides`` maps mode → the model override this request
        dropped, and only :meth:`update` can know it: once the parent row is
        written the retraction is indistinguishable from "never set". See
        :meth:`_sync_model_overrides`.
        """
        desired = self._dedup(desired_user_ids)
        policy = resolve_policy(session, parent)
        current = self._current_members(session, parent)
        current_ids = set(current.keys())
        desired_set = set(desired)
        # Read once, while the session is known good. Both loops below commit
        # per child, so from their second iteration ``parent`` is expired and
        # ``parent.id`` is a query — one their failure handlers must not make.
        parent_id = parent.id
        parent_type = (
            parent.type.value
            if isinstance(parent.type, AICredentialType)
            else str(parent.type)
        )
        parent_admin_credential_id = parent.provider_id

        removed: list[uuid.UUID] = []
        # Collected here and projected once at the end — see
        # :meth:`_project_members` for why the projection is not done inline.
        updated_rows: list[MemberRow] = []
        blocked: list[ManagedReconcileBlock] = []
        revocations: list[RevocationRequest] = []

        key: AICredentialData | None = None

        # ----- Add (desired − current) -----
        # Delegated, not duplicated: ``add_members`` is the same code the
        # auto-provisioning path and "apply to existing" run, so a change to
        # how a member is created cannot land in one of three places.
        # ``claim_held_slots=True``. §5.5 of the ai-credential-providers plan
        # makes automatic provisioning the thing that never steals a default;
        # everything reaching ``reconcile`` is a superuser naming members by
        # hand (create, PATCH, delete-to-empty), which is the same deliberate
        # act as ``apply_to_existing``. It is also what a **manual** record's
        # member list has always done, and §3.2 keeps every control a manual
        # record has today — claiming the slot is one of them, asserted by
        # ``test_claiming_a_slot_resets_the_override_but_holding_one_does_not_wipe_it``.
        # Automatic provisioning does not come through here: it calls
        # ``add_members`` directly and takes the incumbent-wins default.
        addition = self.add_members(
            session,
            parent=parent,
            user_ids=desired,
            actor=admin,
            claim_held_slots=True,
        )
        added = list(addition.added)
        skipped = list(addition.skipped)

        # ----- Remove (current − desired) -----
        # Sorted, so the order in which members are removed — and therefore the
        # order their keys are revoked in — is the same on every run. ``set``
        # iteration order is not, which makes a failure report read differently
        # each time it is produced.
        to_remove = sorted(
            (uid for uid in current_ids if uid not in desired_set), key=str
        )
        for owner_id in to_remove:
            outcome = self._remove_one_member(
                session,
                member=current[owner_id],
                parent_id=parent_id,
                parent_type=parent_type,
                parent_provider_id=parent_admin_credential_id,
                force=force,
            )
            if outcome.block is not None:
                blocked.append(outcome.block)
                continue
            if outcome.revocation is not None:
                revocations.append(outcome.revocation)
            removed.append(owner_id)

        # ----- Update (current ∩ desired) -----
        if apply_fields:
            to_update = [uid for uid in desired if uid in current_ids]
            for owner_id in to_update:
                member = current[owner_id]
                child = member.child
                if child is None:
                    # A member with no key yet (or none any more): there is no
                    # child row to write the parent's fields through to. Skipping
                    # is not a loss — when the key is minted the child is created
                    # from the parent as it stands *then*, so it cannot be stale.
                    continue
                if key_rotated and key is None:
                    key = self._decrypt_parent(session, parent)
                try:
                    child_changed = self._update_child_fields(
                        session, parent, policy, child,
                        key_rotated=key_rotated, key=key,
                        cleared_overrides=cleared_overrides,
                    )
                except HTTPException:
                    raise
                except Exception:
                    # Same rule as the Remove pass above.
                    # ``_update_child_fields`` commits its own writes, so the
                    # rollback discards only the attempt that failed.
                    restore_session(session)
                    logger.exception(
                        "Failed to update child for user %s under parent %s",
                        owner_id, parent_id,
                    )
                    skipped.append(
                        ManagedReconcileSkip(
                            user_id=owner_id, reason="update_failed"
                        )
                    )
                    continue
                if child_changed:
                    updated_rows.append(member)

        # After every row change, and never before one: a revoke destroys a key
        # at the provider, and doing that for a removal the database then refuses
        # is not recoverable. Handed off rather than awaited — reconcile is
        # synchronous and runs on request paths, and a provider that is slow must
        # not make an admin's PATCH slow. A revoke that fails records
        # ``admin.ai_credential.revoke_failed`` against the owner, carrying the
        # external ref, so a leaked key has a name in the audit trail.
        if revocations:
            from app.services.credentials import key_provisioning_service

            key_provisioning_service.schedule_revocations(revocations)

        record = self._to_public(session, parent)
        updated = self._project_members(session, updated_rows)
        return ManagedAICredentialReconcileResult(
            record=record,
            added=added,
            removed=removed,
            updated=updated,
            updated_count=len(updated),
            skipped=skipped,
            blocked=blocked,
        )

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def create(
        self,
        session: Session,
        admin: User,
        data: ManagedAICredentialCreate,
    ) -> ManagedAICredentialReconcileResult:
        """Create the parent row then reconcile to add one member per valid
        target user.

        **Manual records only.** One shared key, copied onto one
        ``AICredential`` child per member, created here. A provider-owned record
        is created by ``AIProvidersService.create``, which writes the provider
        and its one credential in a single transaction — that is what holds the
        1:1 invariant, and it is why this method no longer has a branch for a
        submitted ``provider_id``. The policy fields below are therefore always
        this record's own: nothing here is shadowed, because nothing here has a
        provider to shadow it.
        """
        self._validate_provisioning_shape(session, data)
        encrypted = self._encrypt_key(
            data.type, data.api_key, data.base_url, data.model
        )
        now = datetime.now(timezone.utc)
        parent = ManagedAICredential(
            name=data.name,
            type=data.type,
            encrypted_data=encrypted,
            base_url=data.base_url,
            model=data.model,
            managed_by_id=admin.id,
            created_at=now,
            updated_at=now,
            default_model=self._normalize_default_model(data.default_model),
            available_models=self._normalize_available_models(
                data.available_models
            ),
            set_as_default=data.set_as_default,
            set_user_sdk_defaults=data.set_user_sdk_defaults,
            sdk_default_modes=data.sdk_default_modes,
            model_override_conversation=self._normalize_default_model(
                data.model_override_conversation
            ),
            model_override_building=self._normalize_default_model(
                data.model_override_building
            ),
            expiry_notification_date=data.expiry_notification_date,
        )
        session.add(parent)
        session.commit()
        session.refresh(parent)

        return self.reconcile(
            session, admin, parent, data.target_user_ids,
            apply_fields=False, force=False, key_rotated=False,
        )

    def update(
        self,
        session: Session,
        admin: User,
        managed_credential_id: uuid.UUID,
        data: ManagedAICredentialUpdate,
        force: bool = False,
    ) -> ManagedAICredentialReconcileResult:
        """Update parent scalars (+ rotate the key when ``api_key`` is present)
        then reconcile. Omitting ``target_user_ids`` leaves membership unchanged.

        **A provider-owned record refuses every shadowed field**, with a 400
        naming the provider. Those columns are not read for such a record, so
        accepting the write would save successfully and change nothing — the
        failure mode this whole split exists to remove. Membership, the name and
        the key shape are still editable here; the policy is edited on the
        provider. ``ManagedAICredentialUpdate`` makes every one of those fields
        optional, so "omitted" is representable and the refusal is unambiguous.
        """
        parent = self._get_parent_or_404(session, managed_credential_id)
        policy = resolve_policy(session, parent)

        if data.api_key is not None and policy.is_minted:
            # Refused, not ignored. There is no stored key here to replace, so
            # accepting this would store one nothing reads and leave the admin
            # believing they had rolled a key. Rotating a minted member's key is
            # a per-member mint, not a parent edit.
            #
            # **Checked before the generic provider-owned refusal below**, which
            # also covers ``api_key``: both are correct here, and this one says
            # *why* there is nothing to rotate rather than "go and edit the
            # provider", which for a minted provider is advice that leads
            # nowhere — its rotate-key endpoint refuses too.
            raise HTTPException(
                status_code=400,
                detail=(
                    "This credential mints a separate key for each member and "
                    "holds no key of its own, so there is nothing to rotate."
                ),
            )

        if policy.is_provider_owned:
            self._refuse_shadowed_writes(data, policy)

        # What this record pinned *before* the request, read before a single
        # field is written. It is a transition rather than a state and is not
        # recoverable once the new values are in: a stored ``None`` cannot
        # distinguish "just cleared" from "never set".
        previous_overrides = {
            mode: policy.model_override_for(mode)
            for mode in self._MODE_OVERRIDE_ATTR
        }

        # Apply scalar updates to the parent before reconcile so the diff sees
        # the new desired field values.
        if data.name is not None:
            parent.name = data.name
        if data.base_url is not None:
            parent.base_url = data.base_url
        if data.model is not None:
            parent.model = data.model
        # Curated model metadata. ``default_model``: None = no change. For
        # ``available_models``: None = no change, [] = explicit clear → store [].
        if data.default_model is not None:
            parent.default_model = self._normalize_default_model(
                data.default_model
            )
        if data.available_models is not None:
            parent.available_models = self._normalize_available_models(
                data.available_models
            )
        if data.expiry_notification_date is not None:
            parent.expiry_notification_date = data.expiry_notification_date
        if data.set_as_default is not None:
            parent.set_as_default = data.set_as_default
        if data.set_user_sdk_defaults is not None:
            parent.set_user_sdk_defaults = data.set_user_sdk_defaults
        if data.sdk_default_modes is not None:
            parent.sdk_default_modes = data.sdk_default_modes
        if data.model_override_conversation is not None:
            parent.model_override_conversation = self._normalize_default_model(
                data.model_override_conversation
            )
        if data.model_override_building is not None:
            parent.model_override_building = self._normalize_default_model(
                data.model_override_building
            )

        key_rotated = data.api_key is not None
        if key_rotated:
            parent.encrypted_data = self._encrypt_key(
                parent.type, data.api_key, parent.base_url, parent.model
            )

        parent.updated_at = datetime.now(timezone.utc)
        session.add(parent)
        session.commit()
        session.refresh(parent)

        if data.target_user_ids is not None:
            desired = data.target_user_ids
        else:
            desired = list(self._current_members(session, parent).keys())

        # Modes whose override this request retracted (had a value, now NULL).
        # Passed down because the stored ``None`` alone cannot distinguish
        # "just cleared" from "never set" — see ``_sync_model_overrides``.
        updated_policy = resolve_policy(session, parent)
        cleared_overrides = {
            mode: previous
            for mode, previous in previous_overrides.items()
            if previous is not None
            and updated_policy.model_override_for(mode) is None
        }

        return self.reconcile(
            session, admin, parent, desired,
            apply_fields=True, force=force, key_rotated=key_rotated,
            cleared_overrides=cleared_overrides,
        )

    def delete(
        self,
        session: Session,
        admin: User,
        managed_credential_id: uuid.UUID,
        force: bool = False,
        *,
        allow_provider_owned: bool = False,
    ) -> ManagedAICredentialReconcileResult:
        """Reconcile to empty membership (blast-radius gated) then delete the
        parent row. Returns the reconcile result so the route can surface any
        ``blocked`` members (409) when ``force`` is not set.

        When any member is blocked and ``force`` is False the parent row is left
        in place (delete is aborted).

        **A provider-owned record is refused with a 400 naming the provider**,
        the same shape as :meth:`_refuse_shadowed_writes`, and for a sharper
        reason than symmetry. The FK is ``ON DELETE RESTRICT`` in the other
        direction only, so deleting the credential leaves the ``ai_provider`` row
        standing with its ``auto_provision_roles`` still populated — and
        ``AIProvidersService.auto_provision_targets`` drops a provider that owns
        no credential *silently*, with no skip, no log and no event. Every
        subsequent signup for those roles would get nothing, for ever, while the
        admin surface went on showing the rule as active. Deleting the provider
        is the way to delete its credential, and that path revokes and removes in
        the order §5.4 fixes.

        ``allow_provider_owned`` is how ``AIProvidersService.delete`` reaches
        this method for exactly that purpose. Keyword-only and defaulted off, so
        a new caller has to say the word.
        """
        parent = self._get_parent_or_404(session, managed_credential_id)
        if not allow_provider_owned:
            policy = resolve_policy(session, parent)
            if policy.is_provider_owned:
                provider = policy.provider_name or "its AI provider"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"This credential belongs to the AI provider "
                        f"'{provider}' and cannot be deleted on its own — the "
                        "provider would go on auto-provisioning with nothing to "
                        "grant through. Delete the provider instead; that "
                        "revokes every key it issued first."
                    ),
                )
        parent_id = parent.id
        parent_type = (
            parent.type.value
            if isinstance(parent.type, AICredentialType)
            else str(parent.type)
        )
        parent_admin_credential_id = parent.provider_id

        result = self.reconcile(
            session, admin, parent, [],
            apply_fields=False, force=force, key_rotated=False,
        )

        if result.blocked and not force:
            # Abort: leave the parent + remaining children intact.
            return result

        # ``force`` gets past the blast-radius gate but not past every failure:
        # the Remove pass records ``remove_failed`` for a lock timeout or a
        # constraint trip too, and those members still have their membership row
        # — with the provider handles on it. ``session.delete(parent)`` cascades
        # those rows away, so this is the last moment their keys can be named.
        # Read them here, revoke after the delete commits: the same ordering the
        # Remove pass uses, for the same reason.
        stranded = [
            RevocationRequest(
                user_id=member.membership.user_id,
                parent_id=parent_id,
                provider_admin_credential_id=parent_admin_credential_id,
                provider_type=parent_type,
                external_key_ref=dict(member.membership.external_key_ref),
                audit_user_id=member.membership.user_id,
            )
            for member in self._current_members(session, parent).values()
            if member.membership.external_key_ref
        ]

        session.delete(parent)
        session.commit()

        if stranded:
            from app.services.credentials import key_provisioning_service

            key_provisioning_service.schedule_revocations(stranded)
        return result

    def apply_to_existing(
        self,
        session: Session,
        admin: User,
        managed_credential_id: uuid.UUID,
        *,
        dry_run: bool = False,
    ) -> ManagedAICredentialApplyResult:
        """Grant this credential to every existing account its roles cover.

        ``auto_provision_roles`` fires at account *creation*. Turning it on
        does nothing for the people already on the instance, and re-running
        provisioning on a role change was rejected as a design (a promotion
        should not hand out a company key as a side effect). This is the
        explicit action that closes that gap, and being explicit is the point
        — the admin sees the count before it happens.

        **"Its roles" are the resolved policy's, so a manual record covers
        nobody.** ``policy_from_manual`` returns an empty role list
        unconditionally — the rule lives on ``ai_provider`` — so for a manual
        record this is a truthful ``candidate_count: 0`` rather than an action.
        The reaching caller for real work is ``AIProvidersService.apply_to_existing``,
        which resolves the provider and delegates here.

        Add-only. Nobody loses a credential here: the desired set is current
        members ∪ role matches, never a diff that could remove someone.

        ``dry_run`` returns the candidate list without writing anything, for
        the confirm dialog.
        """
        parent = self._get_parent_or_404(session, managed_credential_id)
        policy = resolve_policy(session, parent)
        roles = policy.auto_provision_roles

        current_ids = set(self._current_members(session, parent).keys())
        candidates: list[ManagedAICredentialApplyCandidate] = []
        matches: list[User] = []
        if roles:
            matches = [
                u
                for u in session.exec(
                    select(User).where(
                        User.is_active == True,  # noqa: E712
                        col(User.role).in_(roles),
                    )
                ).all()
                if u.id not in current_ids
            ]
            candidates = [
                ManagedAICredentialApplyCandidate(
                    user_id=u.id,
                    email=u.email,
                    full_name=u.full_name,
                    role=u.role,
                )
                for u in matches
            ]

        # "12 users will receive this credential" understates what happens
        # when this record wires defaults: each of those users also has a
        # default credential — and its model override — repointed. That is the
        # part an admin would want to know *before* confirming, so it is
        # counted here rather than discovered afterwards.
        overwrites = self._count_default_overwrites(
            session, parent, policy, matches
        )

        if dry_run:
            return ManagedAICredentialApplyResult(
                record=self._to_public(session, parent),
                dry_run=True,
                candidate_count=len(candidates),
                candidates=candidates,
                defaults_overwrite_count=overwrites,
            )

        # ``claim_held_slots=True``: this is the explicit "grant this to
        # everyone who already matches" action, and §5.5 of the
        # ai-credential-providers plan keeps it — with ``set-default-all`` — as
        # the escape hatch that *does* overwrite. Automatic provisioning is the
        # path that never steals a default.
        result = self.add_members(
            session,
            parent=parent,
            user_ids=[c.user_id for c in candidates],
            actor=admin,
            claim_held_slots=True,
        )
        return ManagedAICredentialApplyResult(
            record=self._to_public(session, parent),
            added=result.added,
            skipped=result.skipped,
            dry_run=False,
            candidate_count=len(candidates),
            defaults_overwrite_count=overwrites,
        )

    def _count_default_overwrites(
        self,
        session: Session,
        parent: ManagedAICredential,
        policy: ProvisioningPolicy,
        candidates: list[User],
    ) -> int:
        """How many candidates already hold a default this grant would replace.

        The number behind "…and N of them lose the default they chose" in the
        confirm dialog. A candidate is counted **once** however many things
        they lose — the question the admin is asking is "how many people does
        this disturb", not "how many columns move".

        It has to look at both of the axes ``_add_child`` writes, because they
        are independent flags and an earlier version of this counter looked at
        only one:

        * ``set_user_sdk_defaults`` — for each claimed mode, the owner's
          ``default_ai_credential_<mode>_id`` is repointed and
          ``default_model_override_<mode>`` is *reset*, the latter even when
          the pointer was NULL (see :meth:`_apply_sdk_defaults`, which resets
          the slot wholesale on a claim). So a user with no pointer but a model
          they picked for that mode still loses something, and is counted.
        * ``set_as_default`` — ``ai_credentials_service.set_default`` unsets
          whatever the owner's current default credential *of this type* is and
          rewrites the legacy per-type profile blob. This axis is the one that
          was missing, and it is not a hypothetical: a record with
          ``set_as_default`` on and ``set_user_sdk_defaults`` off previewed as
          costing nothing while demoting every candidate's own key.

        Zero when the record wires no defaults at all — the common, genuinely
        harmless configuration, and the only one the dialog is entitled to
        describe as free.

        One query for the whole candidate list, not one per candidate: this
        runs on a dry run the admin is waiting on.
        """
        if not candidates:
            return 0

        # A type with no SDK engine cannot claim a mode slot — ``_apply_sdk_defaults``
        # returns immediately — so counting one would promise a change that
        # will not happen. The same is true of a slot the owner already holds:
        # this action claims held slots (it is one of the two escape hatches),
        # so those *are* counted; what is not counted is a slot nothing will
        # reach.
        modes: list[str] = []
        if policy.set_user_sdk_defaults and _sdk_engine_for(parent.type):
            modes = [
                mode
                for mode in policy.sdk_default_modes
                if mode in self._MODE_POINTER_ATTR
            ]

        demoted_owner_ids: set[uuid.UUID] = set()
        if policy.set_as_default:
            cred_type = (
                parent.type.value
                if isinstance(parent.type, AICredentialType)
                else parent.type
            )
            demoted_owner_ids = {
                row.owner_id
                for row in session.exec(
                    select(AICredential).where(
                        col(AICredential.owner_id).in_(
                            [user.id for user in candidates]
                        ),
                        AICredential.type == cred_type,
                        AICredential.is_default == True,  # noqa: E712
                    )
                ).all()
            }

        if not modes and not demoted_owner_ids:
            return 0

        count = 0
        for user in candidates:
            if user.id in demoted_owner_ids:
                count += 1
                continue
            if any(
                getattr(user, self._MODE_POINTER_ATTR[mode]) is not None
                or getattr(user, self._MODE_OVERRIDE_ATTR[mode]) is not None
                for mode in modes
            ):
                count += 1
        return count

    def set_default_all(
        self,
        session: Session,
        admin: User,
        managed_credential_id: uuid.UUID,
    ) -> ManagedAICredentialPublic:
        """Set every member's child as its owner's default for the type + flag the
        parent ``set_as_default=True``.

        Members who hold no key yet are skipped rather than failed: there is
        nothing to make default. The parent flag is still set, so the wiring
        happens for them when their key arrives — that is what the flag is for.
        """
        parent = self._get_parent_or_404(session, managed_credential_id)
        policy = resolve_policy(session, parent)
        members = self._current_members(session, parent)
        for owner_id, member in members.items():
            if member.child is None:
                continue
            ai_credentials_service.set_default(session, member.child.id, owner_id)

        # The flag follows ownership. On a provider-owned record it is the
        # provider's column that is read, so writing ``True`` here would leave
        # the action's promise — "and it stays the default for members who
        # arrive later" — unkept while the record displayed the flag as on.
        if policy.is_provider_owned:
            from app.services.credentials.ai_providers_service import (
                ai_providers_service,
            )

            ai_providers_service.write_policy_fields(
                session, policy.provider_id, {"set_as_default": True}
            )
        else:
            parent.set_as_default = True
        parent.updated_at = datetime.now(timezone.utc)
        session.add(parent)
        session.commit()
        session.refresh(parent)
        return self._to_public(session, parent)

    # ------------------------------------------------------------------ #
    # Listing / projection
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Per-key verbs
    # ------------------------------------------------------------------ #
    #
    # The membership row is the resource. It is what carries the vendor handles,
    # names the child credential and holds the provisioning lifecycle, so every
    # verb below addresses one by id and nothing else. Until these existed the
    # only way to act on one person's key was to PATCH the record with the whole
    # desired member set — which is why the blast-radius gate answers with a
    # *list* of blocked members: the caller could never remove fewer than a set.

    def membership_or_404(
        self, session: Session, membership_id: uuid.UUID
    ) -> tuple[ManagedAICredentialMembership, ManagedAICredential]:
        """One membership and the record it belongs to, or 404.

        Both, always, because no verb below can act on one without the other:
        the record carries the provider and the type a revocation has to name.
        """
        membership = session.get(ManagedAICredentialMembership, membership_id)
        if membership is None:
            raise HTTPException(status_code=404, detail="Key not found.")
        parent = session.get(
            ManagedAICredential, membership.managed_credential_id
        )
        if parent is None:  # pragma: no cover - FK guarded
            raise HTTPException(status_code=404, detail="Key not found.")
        return membership, parent

    def revoke_key(
        self,
        session: Session,
        admin: User,
        membership_id: uuid.UUID,
        *,
        force: bool = False,
    ) -> ManagedAICredentialReconcileResult:
        """Take one key away: the same removal a PATCH would do, for one person.

        Returns a reconcile result rather than a bare acknowledgement so the
        route can answer with the envelope the set-based paths already use — the
        blocked list on a refusal, the record on success — and the browser needs
        no second shape for what is the same event.

        On a **shared** record this removes that person's copy and leaves the key
        alone for everyone else, which is what removing a member has always
        meant there. Only a minted membership's revoke reaches the provider.
        """
        membership, parent = self.membership_or_404(session, membership_id)
        child = (
            session.get(AICredential, membership.ai_credential_id)
            if membership.ai_credential_id
            else None
        )
        parent_id = parent.id
        parent_type = (
            parent.type.value
            if isinstance(parent.type, AICredentialType)
            else str(parent.type)
        )
        parent_provider_id = parent.provider_id
        owner_id = membership.user_id

        outcome = self._remove_one_member(
            session,
            member=MemberRow(membership=membership, child=child),
            parent_id=parent_id,
            parent_type=parent_type,
            parent_provider_id=parent_provider_id,
            force=force,
        )
        if outcome.block is not None:
            return ManagedAICredentialReconcileResult(
                record=self._to_public(
                    session,
                    self._get_parent_or_404(session, parent_id),
                ),
                blocked=[outcome.block],
            )
        if outcome.revocation is not None:
            from app.services.credentials import key_provisioning_service

            key_provisioning_service.schedule_revocations([outcome.revocation])
        return ManagedAICredentialReconcileResult(
            record=self._to_public(
                session, self._get_parent_or_404(session, parent_id)
            ),
            removed=[owner_id],
        )

    def set_key_as_holder_default(
        self, session: Session, membership_id: uuid.UUID
    ) -> ManagedAICredentialPublic:
        """Make this key its holder's default for its type.

        The per-person counterpart of *Set default for all*, and deliberately a
        separate verb: the record-level one is an administrator overwriting
        everybody's choice, this one is fixing a single person whose grant
        declined an occupied slot (the incumbent-wins rule) or who changed their
        default later.

        400 when no child credential exists yet — ``pending``, ``minting``,
        ``failed`` and ``suspended`` all mean there is nothing to point a default
        at, and creating something to point at would break the invariant that
        every ``AICredential`` row that exists is usable.
        """
        membership, parent = self.membership_or_404(session, membership_id)
        if membership.ai_credential_id is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This member holds no key yet, so there is nothing to make "
                    "their default."
                ),
            )
        ai_credentials_service.set_default(
            session, membership.ai_credential_id, membership.user_id
        )
        return self._to_public(session, parent)

    # NOTE ON PLACEMENT: everything from here to :meth:`list` is defined
    # *above* it deliberately. ``def list`` binds the name ``list`` in this
    # class body, so every annotation evaluated after that statement resolves
    # ``list[...]`` to the method rather than the builtin and raises at import.
    # Moving this block below it is a ``TypeError: 'function' object is not
    # subscriptable`` at start-up, not a style change.
    # ------------------------------------------------------------------ #
    # The keys list — one row per real API key
    # ------------------------------------------------------------------ #

    def list_keys(
        self,
        session: Session,
        *,
        q: str | None = None,
        statuses: list[MembershipProvisioningStatus] | None = None,
        kind: AdminAIKeyKind | None = None,
        provider_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> AdminAIKeysPublic:
        """One page of the keys list.

        WHY THIS IS NOT :meth:`list` WITH A FLAG
        ----------------------------------------
        :meth:`list` answers "what records exist"; this answers "what keys
        exist". They differ by exactly the thing that broke the admin surface: a
        minted record is **one** row there and **one row per member** here,
        because it holds one key per member. The two questions are projected
        separately rather than one being derived from the other in the browser,
        which is what the old members-as-chips cell was.

        THE COST
        --------
        Flat in the page size and, more importantly, flat in **headcount**. The
        old surface's payload grew with the number of people on a record; this
        one asks:

        1. every parent (bounded by what an admin creates, not by who holds a
           key) and one batched ``resolve_policies`` over them;
        2. one ``COUNT`` and one paged ``SELECT`` over the memberships of minted
           parents, joined to ``user`` because the filter and the sort both need
           it;
        3. one lookup of the page's child credentials, one batched onboarding
           state, and — only when shared rows are on the page — one grouped
           member count.

        ORDERING: SHARED ROWS FIRST, THEN PER-USER, EACH BY NAME
        --------------------------------------------------------
        The two row sources live in different tables, so a page is a slice of
        their union. Making ``kind`` the **primary** sort key is what keeps that
        slice exact with one query: the shared rows are few and fully in memory,
        so a page either starts inside them and is topped up from the membership
        page, or lies entirely past them at a known offset. Interleaving the two
        by name instead would need either a SQL ``UNION`` over two dissimilar
        tables or a fetch window sized by the shared count, and buys nothing an
        administrator can use — a specific key is found by searching, not by
        scrolling to where it sorts.

        FILTERS
        -------
        ``q`` matches the holder's email or name **or the credential's name**,
        so "openai" lists everybody's OpenAI keys and "alice" lists Alice's.
        ``statuses`` is a provisioning filter and therefore excludes every
        shared row by construction: a shared key has no provisioning lifecycle,
        and inventing a status for it so it could pass the filter is exactly the
        synthetic value :class:`AdminAIKeyRow` refuses to publish.
        """
        from sqlmodel import func, or_

        limit = max(1, min(limit, 100))
        skip = max(0, skip)
        needle = f"%{q.strip()}%" if q and q.strip() else None

        parents = session.exec(select(ManagedAICredential)).all()
        policies = resolve_policies(session, parents)
        parents_by_id = {parent.id: parent for parent in parents}

        minted_ids: list[uuid.UUID] = []
        shared_parents: list[ManagedAICredential] = []
        for parent in parents:
            policy = policies[parent.id]
            if provider_id is not None and policy.provider_id != provider_id:
                continue
            if policy.is_minted:
                minted_ids.append(parent.id)
            else:
                shared_parents.append(parent)

        # ---- The shared rows: one per record, all in memory ----
        shared_rows: list[AdminAIKeyRow] = []
        if kind is not AdminAIKeyKind.PER_USER and not statuses:
            wanted = [
                parent
                for parent in shared_parents
                if needle is None
                or q.strip().lower() in parent.name.lower()
            ]
            member_counts = self._member_counts(
                session, [parent.id for parent in wanted]
            )
            shared_rows = [
                self._shared_key_row(
                    parent, policies[parent.id], member_counts.get(parent.id, 0)
                )
                for parent in wanted
            ]
            shared_rows.sort(key=lambda row: row.credential_name.lower())

        # ---- The per-user rows: one per membership of a minted parent ----
        per_user_total = 0
        memberships: list[tuple[ManagedAICredentialMembership, User]] = []
        if kind is not AdminAIKeyKind.SHARED and minted_ids:
            clauses = [
                col(ManagedAICredentialMembership.managed_credential_id).in_(
                    minted_ids
                )
            ]
            if statuses:
                clauses.append(
                    col(ManagedAICredentialMembership.status).in_(
                        [status.value for status in statuses]
                    )
                )
            if needle is not None:
                name_matched = [
                    parent_id
                    for parent_id in minted_ids
                    if q.strip().lower()
                    in parents_by_id[parent_id].name.lower()
                ]
                match_clauses = [
                    col(User.email).ilike(needle),
                    col(User.full_name).ilike(needle),
                ]
                if name_matched:
                    match_clauses.append(
                        col(
                            ManagedAICredentialMembership.managed_credential_id
                        ).in_(name_matched)
                    )
                clauses.append(or_(*match_clauses))

            # The join is inner on purpose. A membership whose user row has gone
            # is unreachable through the FK (``ON DELETE CASCADE``) and is
            # dropped here rather than rendered as a key belonging to nobody —
            # the same call ``_to_public`` makes, where it is logged as
            # defensive.
            count_statement = (
                select(func.count())
                .select_from(ManagedAICredentialMembership)
                .join(
                    User,
                    col(User.id) == col(ManagedAICredentialMembership.user_id),
                )
            )
            for clause in clauses:
                count_statement = count_statement.where(clause)
            per_user_total = session.exec(count_statement).one()

            # Where this page starts inside the per-user sequence. The shared
            # rows come first as a block, so a page either overlaps them (offset
            # 0 here, topped up below) or begins past them.
            per_user_skip = max(0, skip - len(shared_rows))
            per_user_limit = limit - max(
                0, min(len(shared_rows) - skip, limit)
            )
            if per_user_limit > 0:
                statement = (
                    select(ManagedAICredentialMembership, User)
                    .join(
                        User,
                        col(User.id)
                        == col(ManagedAICredentialMembership.user_id),
                    )
                )
                for clause in clauses:
                    statement = statement.where(clause)
                statement = (
                    statement.order_by(
                        func.lower(col(User.email)),
                        col(ManagedAICredentialMembership.id),
                    )
                    .offset(per_user_skip)
                    .limit(per_user_limit)
                )
                memberships = list(session.exec(statement).all())

        rows = shared_rows[skip : skip + limit]
        if memberships:
            rows = rows + self._per_user_key_rows(
                session, memberships, parents_by_id, policies
            )
        return AdminAIKeysPublic(
            data=rows, count=len(shared_rows) + per_user_total
        )

    @staticmethod
    def _member_counts(
        session: Session, parent_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """How many people hold each of these records, in one grouped query.

        Named rather than inlined because the obvious implementation —
        ``len(self._current_members(...))`` per row — is a per-row pair of
        queries, which is the cost the keys list exists to remove.
        """
        from sqlmodel import func

        if not parent_ids:
            return {}
        rows = session.exec(
            select(
                col(ManagedAICredentialMembership.managed_credential_id),
                func.count(),
            )
            .where(
                col(ManagedAICredentialMembership.managed_credential_id).in_(
                    parent_ids
                )
            )
            .group_by(col(ManagedAICredentialMembership.managed_credential_id))
        ).all()
        return {parent_id: count for parent_id, count in rows}

    @staticmethod
    def _shared_key_row(
        parent: ManagedAICredential,
        policy: ProvisioningPolicy,
        member_count: int,
    ) -> AdminAIKeyRow:
        """The one row a shared record contributes.

        No status, no holder and no key reference: this key was pasted, not
        minted, so there is no provisioning lifecycle to report, no single
        person to name and no vendor handle we were ever given.
        """
        return AdminAIKeyRow(
            kind=AdminAIKeyKind.SHARED,
            managed_credential_id=parent.id,
            membership_id=None,
            credential_name=parent.name,
            type=parent.type,
            provider_id=policy.provider_id,
            provider_name=policy.provider_name,
            member_count=member_count,
            created_at=parent.created_at,
        )

    def _per_user_key_rows(
        self,
        session: Session,
        memberships: list[tuple[ManagedAICredentialMembership, User]],
        parents_by_id: dict[uuid.UUID, ManagedAICredential],
        policies: dict[uuid.UUID, ProvisioningPolicy],
    ) -> list[AdminAIKeyRow]:
        """Project one page of memberships, in a fixed number of queries.

        Two lookups for the whole page — the children the memberships name, and
        the batched onboarding state — for the same reason
        :meth:`_project_members` exists: a required keyword does not stop a
        caller satisfying it inside a loop, and an N+1 here would arrive exactly
        when an admin is watching a batch provision.
        """
        child_ids = [
            membership.ai_credential_id
            for membership, _owner in memberships
            if membership.ai_credential_id
        ]
        children: dict[uuid.UUID, AICredential] = {}
        if child_ids:
            children = {
                row.id: row
                for row in session.exec(
                    select(AICredential).where(
                        col(AICredential.id).in_(child_ids)
                    )
                ).all()
            }
        key_states = self._owner_key_states(
            session, {owner.id for _membership, owner in memberships}
        )

        rows: list[AdminAIKeyRow] = []
        for membership, owner in memberships:
            parent = parents_by_id[membership.managed_credential_id]
            policy = policies[parent.id]
            child = (
                children.get(membership.ai_credential_id)
                if membership.ai_credential_id
                else None
            )
            rows.append(
                AdminAIKeyRow(
                    kind=AdminAIKeyKind.PER_USER,
                    managed_credential_id=parent.id,
                    membership_id=membership.id,
                    credential_name=parent.name,
                    type=parent.type,
                    provider_id=policy.provider_id,
                    provider_name=policy.provider_name,
                    holder_user_id=owner.id,
                    holder_email=owner.email,
                    holder_full_name=owner.full_name,
                    provisioning_status=MembershipProvisioningStatus(
                        membership.status
                    ),
                    provision_error=membership.last_error,
                    provision_attempts=membership.provision_attempts,
                    child_credential_id=membership.ai_credential_id,
                    is_default=child.is_default if child else False,
                    api_key_onboarding_state=key_states.get(
                        owner.id, AIKeyOnboardingState.NEEDS_KEY
                    ),
                    key_reference=self._key_reference(
                        membership.external_key_ref
                    ),
                    created_at=membership.created_at,
                )
            )
        return rows

    @staticmethod
    def _key_reference(external_key_ref: dict | None) -> str | None:
        """The vendor's handles for one key, formatted for a person to read.

        Handles only — the project and the service account — so a row can be
        matched against the provider's own console. Never the secret, and never
        the api-key id: that one names the credential at the vendor and has no
        job on a screen.
        """
        if not external_key_ref:
            return None
        parts = [
            str(external_key_ref.get(field))
            for field in ("project_id", "service_account_id")
            if external_key_ref.get(field)
        ]
        return " · ".join(parts) or None

    def list(
        self,
        session: Session,
        admin: User,
        managed_by_id: uuid.UUID | None = None,
        target_user_id: uuid.UUID | None = None,
    ) -> list[ManagedAICredentialPublic]:
        """List parent records fleet-wide, optionally filtered by managing admin
        and/or by a member user."""
        statement = select(ManagedAICredential)
        if managed_by_id is not None:
            statement = statement.where(
                ManagedAICredential.managed_by_id == managed_by_id
            )
        statement = statement.order_by(ManagedAICredential.created_at.desc())
        parents = session.exec(statement).all()

        if target_user_id is not None:
            # Keep only parents that have this user as a member — read from the
            # membership rows, not from their credentials. The difference is
            # visible: someone whose minted key has not arrived yet, or has
            # failed, is a member and must appear here. Deriving this from
            # ``AICredential`` again would answer "who holds a key", which is a
            # different question that happens to have had the same answer.
            parent_ids = {
                row.managed_credential_id
                for row in session.exec(
                    select(ManagedAICredentialMembership).where(
                        ManagedAICredentialMembership.user_id == target_user_id
                    )
                ).all()
            }
            parents = [p for p in parents if p.id in parent_ids]

        # One provider fetch for the whole page rather than one per record —
        # the batch form ``resolve_policies`` exists for exactly this call.
        policies = resolve_policies(session, parents)
        return [
            self._to_public(session, p, policy=policies[p.id])
            for p in parents
        ]

    def get(
        self,
        session: Session,
        admin: User,
        managed_credential_id: uuid.UUID,
    ) -> ManagedAICredentialPublic:
        parent = self._get_parent_or_404(session, managed_credential_id)
        return self._to_public(session, parent)

    def _to_public(
        self,
        session: Session,
        parent: ManagedAICredential,
        *,
        policy: ProvisioningPolicy | None = None,
    ) -> ManagedAICredentialPublic:
        """Load memberships + children + owners and build the member projection.

        **Nothing here is asked once per member.** Memberships, their children,
        their owners, the batched onboarding-state lookup and the owning
        provider are each asked for once per *record*, so the admin list page's
        cost grows with the number of records, as it always has, and not with
        how many people hold them. That matters more than it used to: a minted
        record's members appear the moment they are added, before any key
        exists, so a projection with a per-member lookup in it would get slower
        exactly when an admin is watching a batch provision.

        What is *tested* is narrower than what is claimed, and the gap is worth
        knowing: ``minted_ai_credentials_test.py::
        test_the_member_list_costs_the_same_however_many_members_there_are``
        counts two needles — the membership read and the ``is_default``
        predicate — and holds them flat across member counts. The children,
        owner and provider reads are flat by inspection, not by that test.

        The provider read is the newest of them, and :meth:`list` folds it into
        one for the whole page by resolving policies in a batch and handing the
        answer in; a caller that does not is charged one provider read per
        record, which is the cost this method had before the batch existed.

        The projection is also the **only** thing that reads provisioning state.
        The client is handed one member list with a status on each entry; it
        never sees that a status lives on one row and a key on another, and it
        therefore cannot invent a rule for combining them.
        """
        members_by_owner = self._current_members(session, parent)
        # Resolved once per record — or handed in already resolved by ``list``,
        # which batches the provider fetch for the whole page.
        if policy is None:
            policy = resolve_policy(session, parent)

        # One query for every owner rather than ``session.get`` per member.
        owner_ids = set(members_by_owner.keys())
        owners: dict[uuid.UUID, User] = {}
        if owner_ids:
            owners = {
                row.id: row
                for row in session.exec(
                    select(User).where(col(User.id).in_(owner_ids))
                ).all()
            }

        # One batched lookup for the whole list. The member projection has a
        # query-count test precisely because a per-member question here is how a
        # cheap predicate turns into a reason not to use it.
        key_states = self._owner_key_states(session, owner_ids)

        members: list[ManagedAICredentialMember] = []
        for owner_id, member in members_by_owner.items():
            owner = owners.get(owner_id)
            if owner is None:
                # The user row is gone but the membership survived — impossible
                # through the FK (``ON DELETE CASCADE``), so this is defensive.
                # Logged rather than skipped in silence: ``member_count`` is
                # ``len(members)``, so a dropped row makes the admin's member
                # count quietly wrong, and a count that disagrees with reality
                # for a reason nobody recorded is the hardest kind to chase.
                logger.warning(
                    "Membership %s of parent %s names user %s, which does not "
                    "exist; omitted from the member list.",
                    member.membership.id, parent.id, owner_id,
                )
                continue
            members.append(
                self._member_dto(
                    member,
                    owner,
                    key_state=key_states.get(
                        owner_id, AIKeyOnboardingState.NEEDS_KEY
                    ),
                )
            )

        # Derive is_oauth_token from the parent's stored key. The adapter owns
        # both halves: whether this provider can hold an OAuth token at all, and
        # what one looks like. The first half is checked before the decrypt —
        # this runs once per parent on every fleet-table list, so decrypting a
        # provider that cannot hold one would be pure cost. A minted parent has
        # no key to classify and is skipped for the same reason.
        is_oauth = False
        adapter = registry.find_adapter(parent.type)
        if (
            adapter is not None
            and adapter.issues_oauth_tokens
            and not policy.is_minted
            and (parent.encrypted_data or policy.is_provider_owned)
        ):
            try:
                api_key = self._decrypt_parent(session, parent).api_key or ""
                is_oauth = adapter.classify_key(api_key).is_oauth_token
            except Exception:  # pragma: no cover - defensive
                is_oauth = False

        return ManagedAICredentialPublic(
            id=parent.id,
            name=parent.name,
            type=parent.type,
            base_url=parent.base_url,
            model=parent.model,
            default_model=policy.default_model,
            available_models=policy.available_models,
            set_as_default=policy.set_as_default,
            set_user_sdk_defaults=policy.set_user_sdk_defaults,
            sdk_default_modes=policy.sdk_default_modes,
            model_override_conversation=policy.model_override_conversation,
            model_override_building=policy.model_override_building,
            expiry_notification_date=policy.expiry_notification_date,
            managed_by_id=parent.managed_by_id,
            provisioning_mode=policy.provisioning_mode,
            provider_id=policy.provider_id,
            provider_name=policy.provider_name,
            is_provider_owned=policy.is_provider_owned,
            # True when a key exists for this record to hand out, wherever it
            # is stored: on the record for a manual one, on the provider for a
            # ``fixed_key`` one. False for a minted record, whose members' keys
            # are created individually and none of which is *this* record's.
            # ``provider_name is not None`` is what distinguishes a resolved
            # provider from the "provider-owned but the row is gone" policy: for
            # the latter there is no key anywhere, and answering True would tell
            # the admin a record holds one when nothing does.
            has_api_key=(
                bool(parent.encrypted_data)
                or (
                    policy.is_provider_owned
                    and policy.provider_name is not None
                    and not policy.is_minted
                )
            ),
            is_oauth_token=is_oauth,
            members=members,
            member_count=len(members),
            created_at=parent.created_at,
            updated_at=parent.updated_at,
        )

    # ------------------------------------------------------------------ #
    # Parent-aware test connection key resolution
    # ------------------------------------------------------------------ #

    def resolve_test_key(
        self,
        session: Session,
        managed_credential_id: uuid.UUID,
    ) -> AICredentialData:
        """Decrypt the parent's stored key for the Test Connection edit case
        (blank api_key on an existing record). 404 if the parent is missing."""
        parent = self._get_parent_or_404(session, managed_credential_id)
        return self._decrypt_parent(session, parent)


# Singleton instance (matches ai_credentials_service).
managed_ai_credentials_service = ManagedAICredentialsService()
