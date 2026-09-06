"""
Managed AI Credentials Service.

Owns the **parent** ``ManagedAICredential`` record and the **reconcile** routine
that diffs a desired target-user set against the actual child ``AICredential``
rows (those whose ``managed_credential_id`` points at the parent).

Children remain ordinary per-user ``AICredential`` rows that look EXACTLY like
today's admin-managed credentials to the rest of the system. This service does
NOT duplicate encryption, per-type validation, one-default-per-type, profile
auto-sync, or blast-radius logic — every per-child create/update/delete/
set-default is delegated to :data:`ai_credentials_service`. After delegating to
``create_credential`` the new child is stamped with ``is_admin_managed=True``,
``managed_by_id=parent.managed_by_id`` and ``managed_credential_id=parent.id`` so
it is structurally linked back to the parent (and reachable as a member).

Membership is **derived** from the children — there is no ``target_user_ids``
column on the parent. A failed add simply isn't a member (self-healing on the
next reconcile).
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
    VALID_AUTO_PROVISION_ROLES,
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
from app.models.users.user import User
from app.services.credentials.ai_credentials_service import (
    AICredentialInUseError,
    ai_credentials_service,
)
from app.services.environments.model_catalog import _strip_provider_prefix
from app.services.environments.sdk_constants import (
    is_credential_compatible_with_sdk,
)
from app.utils import restore_session

logger = logging.getLogger(__name__)


# Which SDK engine string to compose for a credential type, per mode (mirrors
# admin_ai_credentials_service._TYPE_TO_SDK_ENGINE / the AddEnvironment SDK
# composition): claude-code for anthropic/minimax, opencode/<provider> for the
# OpenCode-only providers.
_TYPE_TO_SDK_ENGINE: dict[AICredentialType, str] = {
    AICredentialType.ANTHROPIC: "claude-code/anthropic",
    AICredentialType.MINIMAX: "claude-code/minimax",
    AICredentialType.OPENAI: "opencode/openai",
    AICredentialType.GOOGLE: "opencode/google",
    AICredentialType.OPENAI_COMPATIBLE: "opencode/openai_compatible",
}


class ManagedCredentialConflictError(Exception):
    """Two auto-provisioned parents would fight over the same default slot.

    ``default_ai_credential_<mode>_id`` holds exactly one credential. If two
    managed records both auto-provision to the same role AND both wire that
    role's accounts' SDK default for the same mode, the account gets whichever
    one happened to be provisioned second — silently, differently per user if
    the row order ever changes, and invisibly to the admin who configured
    both. So the second configuration is refused at write time rather than
    resolved at grant time.

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
class MemberAddition:
    """What one add-only membership grant did.

    Deliberately not a ``ManagedAICredentialReconcileResult``: see
    :meth:`ManagedAICredentialsService.add_members`.
    """

    added: list[ManagedAICredentialMember] = field(default_factory=list)
    skipped: list[ManagedReconcileSkip] = field(default_factory=list)


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

    def _decrypt_parent(self, parent: ManagedAICredential) -> AICredentialData:
        """Decrypt the parent's canonical key (same codec as a child row)."""
        from app.core.security import decrypt_field

        data_dict = json.loads(decrypt_field(parent.encrypted_data))
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
    ) -> dict[uuid.UUID, AICredential]:
        """Map ``owner_id -> child credential`` for the parent's children."""
        rows = session.exec(
            select(AICredential).where(
                AICredential.managed_credential_id == parent.id
            )
        ).all()
        return {row.owner_id: row for row in rows}

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

    @staticmethod
    def _normalize_auto_provision_roles(value: list[str] | None) -> list[str] | None:
        """Validate + canonicalise an ``auto_provision_roles`` list.

        ``None`` passes through as "no change". Anything else is trimmed,
        de-duplicated order-preservingly, and checked against the role enum —
        an unknown role is a 400, not a silent drop, because an admin who
        mistypes a role would otherwise save successfully and watch nothing
        happen at the next signup with no clue why.
        """
        if value is None:
            return None
        seen: set[str] = set()
        out: list[str] = []
        for raw in value:
            entry = (raw or "").strip()
            if not entry or entry in seen:
                continue
            if entry not in VALID_AUTO_PROVISION_ROLES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown role '{entry}' in auto_provision_roles. "
                        f"Valid roles: {', '.join(VALID_AUTO_PROVISION_ROLES)}."
                    ),
                )
            seen.add(entry)
            out.append(entry)
        return out

    @classmethod
    def _claimed_slots(
        cls,
        roles: list[str] | None,
        modes: list[str] | None,
        set_user_sdk_defaults: bool,
    ) -> set[tuple[str, str]]:
        """The ``(role, mode)`` default slots a configuration lays claim to.

        Empty unless the record does all three things: auto-provision to a
        role, wire SDK defaults, and claim a mode. A parent that
        auto-provisions without ``set_user_sdk_defaults`` grants a credential
        and touches no default, so any number of those may coexist — that is a
        supported configuration, not an oversight.

        Modes outside the two real ones are filtered out, because a mode that
        wires nothing cannot be fought over. This is the one place the rewrite
        is not merely a restatement of the older role-and-mode intersection,
        which would have flagged two records sharing an invalid mode string.
        It is also downstream of a real gap: ``sdk_default_modes`` is not
        validated at the edge the way ``auto_provision_roles`` is (see
        ``_normalize_auto_provision_roles``), so a typo saves successfully and
        then wires nothing, with no error anywhere.
        """
        if not set_user_sdk_defaults or not roles or not modes:
            return set()
        wanted_modes = [m for m in modes if m in cls._MODE_OVERRIDE_ATTR]
        return {(role, mode) for role in roles for mode in wanted_modes}

    def _validate_auto_provision_uniqueness(
        self,
        session: Session,
        parent_id: uuid.UUID | None,
        roles: list[str],
        modes: list[str],
        set_user_sdk_defaults: bool,
        *,
        previously_claimed: set[tuple[str, str]] | None = None,
    ) -> None:
        """Refuse a second auto-provisioned owner of a ``(role, mode)`` slot.

        ``User.default_ai_credential_<mode>_id`` holds exactly one credential,
        so two records that would both wire it for the same role fight over it
        silently, per user, in row order. The second configuration is refused
        at write time instead.

        **The rule is scoped to the transition, not to the state.** Only slots
        this request *newly* claims — ``claimed - previously_claimed`` — can
        raise. A record already sitting in a conflicting configuration is not
        made this request's problem by being touched: renaming it, rotating its
        key or editing an unrelated field must not 409 on a collision the admin
        did not introduce. (Phase 1 reached the same answer for lockout
        validation; it is the feature's rule, not a local patch here.)

        The alternative — validating the effective end state — reads as safer
        and is not. It makes a stale absolute payload indistinguishable from a
        deliberate edit, so a client rebuilding its request from an open-time
        snapshot gets refused for someone else's change; and a validator taught
        to tolerate that is one that would also have masked the membership
        clobber ``target_user_ids`` had. The dialog sends only what the admin
        touched, and this is the backstop under it.

        ``parent_id`` is the record being written (excluded from the search),
        or ``None`` on create. ``previously_claimed`` is what that record
        claimed *before* this request; empty on create, where every slot is
        new.

        Raises :class:`ManagedCredentialConflictError`; the route maps it to a
        409 naming the other record.
        """
        claimed = self._claimed_slots(roles, modes, set_user_sdk_defaults)
        newly_claimed = claimed - (previously_claimed or set())
        if not newly_claimed:
            return

        for other in session.exec(select(ManagedAICredential)).all():
            if parent_id is not None and other.id == parent_id:
                continue
            overlap = newly_claimed & self._claimed_slots(
                other.auto_provision_roles,
                other.sdk_default_modes,
                other.set_user_sdk_defaults,
            )
            if not overlap:
                continue
            role, mode = sorted(overlap)[0]
            raise ManagedCredentialConflictError(
                conflicting_name=other.name,
                conflicting_id=other.id,
                role=role,
                mode=mode,
            )

    # ------------------------------------------------------------------ #
    # Per-child operations (delegate to ai_credentials_service)
    # ------------------------------------------------------------------ #

    def _stamp_child(
        self,
        session: Session,
        child: AICredential,
        parent: ManagedAICredential,
    ) -> None:
        """Stamp the admin-managed markers + structural parent link on a freshly
        created child so it looks exactly like today's admin-managed rows.

        Also writes through the parent's admin-curated model metadata
        (``default_model`` / ``available_models``) directly on the child row —
        these are non-secret plain columns, so no ``update_credential`` round-trip
        is needed (they are not part of the encrypted ``AICredentialData``)."""
        child.is_admin_managed = True
        child.managed_by_id = parent.managed_by_id
        child.managed_credential_id = parent.id
        child.default_model = parent.default_model
        child.available_models = parent.available_models
        session.add(child)
        session.commit()
        session.refresh(child)

    @staticmethod
    def _model_override_for(
        parent: ManagedAICredential, mode: str
    ) -> str | None:
        """The parent's model override for ``mode`` (``None`` when unset)."""
        if mode == "conversation":
            return parent.model_override_conversation
        return parent.model_override_building

    def _apply_sdk_defaults(
        self,
        session: Session,
        owner: User,
        child: AICredential,
        parent: ManagedAICredential,
    ) -> None:
        """Wire the owner's ``default_sdk_*`` + ``default_ai_credential_*_id`` for
        the parent's ``sdk_default_modes``. A mode whose composed engine is
        incompatible with the type is skipped (not a hard error).

        The per-mode model override is written here too, and written
        *unconditionally* for every mode this parent claims — including back
        to ``NULL`` when the parent has no override of its own.

        That is a reset, not a wipe, because of *when* this runs: only from
        ``_add_child``, i.e. only when the owner was not already a member, so
        the credential pointer for this mode is moving onto a different
        credential than the one it named before. Whatever override was sitting
        in the slot described that other credential and may well name a model
        this provider does not serve — an OpenAI model id left pinned in front
        of a freshly wired Anthropic key. Carrying it across would be the
        silent breakage; clearing it falls back to the credential's own
        ``default_model``.

        The update path is the opposite case and behaves the opposite way —
        see :meth:`_sync_model_overrides`.
        """
        sdk_engine = _TYPE_TO_SDK_ENGINE.get(child.type)
        if not sdk_engine:
            return

        for mode in parent.sdk_default_modes:
            if mode not in ("conversation", "building"):
                continue
            if not is_credential_compatible_with_sdk(sdk_engine, child.type):
                continue
            override = self._model_override_for(parent, mode)
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
        parent: ManagedAICredential,
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
        if not parent.set_user_sdk_defaults:
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
        for mode in parent.sdk_default_modes:
            if mode not in self._MODE_OVERRIDE_ATTR:
                continue
            if getattr(owner, self._MODE_POINTER_ATTR[mode]) != child.id:
                continue
            attr = self._MODE_OVERRIDE_ATTR[mode]
            override = self._model_override_for(parent, mode)
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
        owner: User,
        key: AICredentialData,
    ) -> AICredential:
        """Create one child for ``owner`` via the per-user pipeline, then stamp
        it + apply optional default / SDK-default wiring.

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
                expiry_notification_date=parent.expiry_notification_date,
            ),
        )
        child = session.get(AICredential, public.id)
        self._stamp_child(session, child, parent)

        # Snapshotted while the session is known good. ``_stamp_child`` has
        # just committed, which expires the identity map, so every one of
        # these is a lazy load from here on — and a lazy load is a query the
        # handler below cannot afford to make.
        child_id = child.id
        owner_id = owner.id
        parent_id = parent.id

        # --- Post-commit wiring: best-effort, never demotes a created member. ---
        try:
            if parent.set_as_default:
                ai_credentials_service.set_default(session, child.id, owner.id)
                session.refresh(child)
            if parent.set_user_sdk_defaults:
                self._apply_sdk_defaults(session, owner, child, parent)
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

        return child

    def _update_child_fields(
        self,
        session: Session,
        parent: ManagedAICredential,
        child: AICredential,
        *,
        key_rotated: bool,
        key: AICredentialData | None,
        cleared_overrides: dict[str, str] | None = None,
    ) -> bool:
        """Write changed parent scalar fields (and rotated key) through to a
        child via the per-user pipeline, then apply/clear default per
        ``set_as_default``.

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
            parent.expiry_notification_date != child.expiry_notification_date
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
                    parent.expiry_notification_date
                    if (expiry_changed and parent.expiry_notification_date is not None)
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
        if expiry_changed and parent.expiry_notification_date is None:
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
        if parent.default_model != child.default_model:
            child.default_model = parent.default_model
            curated_changed = True
        if parent.available_models != child.available_models:
            child.available_models = parent.available_models
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
            session, parent, child, cleared_overrides
        ):
            changed = True

        # Default flag application/clear (counts as a change of its own).
        if parent.set_as_default and not child.is_default:
            ai_credentials_service.set_default(session, child.id, child.owner_id)
            session.refresh(child)
            changed = True
        elif not parent.set_as_default and child.is_default:
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
    ) -> MemberAddition:
        """Grant ``parent`` to ``user_ids``. Adds only — never removes.

        This is the Add pass of :meth:`reconcile`, lifted out because three
        callers need exactly it and nothing else: ``reconcile`` itself,
        ``AccountProvisioningService`` when an account is created, and
        "apply to existing users". Copying it would have been the third place
        the "validate active → decrypt → create child → wire defaults"
        sequence lives, and the one that quietly falls behind.

        Idempotent: ids that are already members are not re-added and are not
        reported — being a member is the outcome the caller asked for.

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
        current_ids = set(self._current_members(session, parent).keys())

        added: list[ManagedAICredentialMember] = []
        skipped: list[ManagedReconcileSkip] = []
        key: AICredentialData | None = None

        for owner_id in [uid for uid in desired if uid not in current_ids]:
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
            if key is None:
                key = self._decrypt_parent(parent)
            try:
                child = self._add_child(session, parent, owner, key)
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
                skipped.append(
                    ManagedReconcileSkip(
                        user_id=owner_id, reason="provision_failed"
                    )
                )
                # The decrypted key is a plain value, not session state, so it
                # survives the rollback and the remaining owners are still
                # attempted.
                continue
            added.append(
                ManagedAICredentialMember(
                    user_id=owner.id,
                    email=owner.email,
                    full_name=owner.full_name,
                    child_credential_id=child.id,
                    is_default=child.is_default,
                )
            )

        return MemberAddition(added=added, skipped=skipped)

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
        current = self._current_members(session, parent)
        current_ids = set(current.keys())
        desired_set = set(desired)
        # Read once, while the session is known good. Both loops below commit
        # per child, so from their second iteration ``parent`` is expired and
        # ``parent.id`` is a query — one their failure handlers must not make.
        parent_id = parent.id

        removed: list[uuid.UUID] = []
        updated: list[ManagedAICredentialMember] = []
        blocked: list[ManagedReconcileBlock] = []

        key: AICredentialData | None = None

        # ----- Add (desired − current) -----
        # Delegated, not duplicated: ``add_members`` is the same code the
        # auto-provisioning path and "apply to existing" run, so a change to
        # how a member is created cannot land in one of three places.
        addition = self.add_members(
            session, parent=parent, user_ids=desired, actor=admin
        )
        added = list(addition.added)
        skipped = list(addition.skipped)

        # ----- Remove (current − desired) -----
        to_remove = [uid for uid in current_ids if uid not in desired_set]
        for owner_id in to_remove:
            child = current[owner_id]
            # Which of the owner's default slots this child holds, read while
            # the row still exists. After the delete the pointers are already
            # NULL (``ondelete="SET NULL"``) and the answer is unrecoverable.
            # See :meth:`_release_model_overrides`.
            child_id = child.id
            owner = session.get(User, owner_id)
            held_modes = (
                self._modes_pointing_at(owner, child_id) if owner else []
            )
            try:
                ai_credentials_service.delete_credential(
                    session,
                    child.id,
                    child.owner_id,
                    force=force,
                    admin_override=True,
                )
            except AICredentialInUseError as in_use:
                blocked.append(
                    ManagedReconcileBlock(
                        user_id=owner_id,
                        reason="in_use_bundle",
                        impact=in_use.impact.model_dump(mode="json"),
                    )
                )
                continue
            except HTTPException:
                raise
            except Exception:
                # Repair before logging, and before continuing — the same rule
                # ``add_members`` states at length. A statement-level failure
                # here leaves the transaction aborted, and this function ends
                # at ``_to_public``, which queries: recording a block and
                # carrying on would turn a per-member problem into a 500 for
                # the whole PATCH. Nothing of the caller's is pending —
                # ``delete_credential`` and ``_release_model_overrides`` commit
                # their own work, as does the Add pass above.
                restore_session(session)
                logger.exception(
                    "Failed to remove child for user %s under parent %s",
                    owner_id, parent_id,
                )
                blocked.append(
                    ManagedReconcileBlock(
                        user_id=owner_id, reason="remove_failed", impact=None
                    )
                )
                continue
            # Only on the success path: a blocked member keeps their child and
            # must keep their wiring with it.
            self._release_model_overrides(session, owner_id, held_modes)
            removed.append(owner_id)

        # ----- Update (current ∩ desired) -----
        if apply_fields:
            to_update = [uid for uid in desired if uid in current_ids]
            for owner_id in to_update:
                child = current[owner_id]
                if key_rotated and key is None:
                    key = self._decrypt_parent(parent)
                try:
                    child_changed = self._update_child_fields(
                        session, parent, child,
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
                    owner = session.get(User, owner_id)
                    updated.append(
                        ManagedAICredentialMember(
                            user_id=owner_id,
                            email=owner.email if owner else "",
                            full_name=owner.full_name if owner else None,
                            child_credential_id=child.id,
                            is_default=child.is_default,
                        )
                    )

        record = self._to_public(session, parent)
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
        """Create the parent row (validate + encrypt the canonical key) then
        reconcile to create one child per valid target user."""
        encrypted = self._encrypt_key(
            data.type, data.api_key, data.base_url, data.model
        )
        auto_roles = (
            self._normalize_auto_provision_roles(data.auto_provision_roles)
            or []
        )
        # Before the row exists: a conflict must not leave a half-configured
        # parent behind for the admin to clean up.
        self._validate_auto_provision_uniqueness(
            session,
            None,
            auto_roles,
            data.sdk_default_modes,
            data.set_user_sdk_defaults,
        )
        now = datetime.now(timezone.utc)
        parent = ManagedAICredential(
            name=data.name,
            type=data.type,
            encrypted_data=encrypted,
            base_url=data.base_url,
            model=data.model,
            default_model=self._normalize_default_model(data.default_model),
            available_models=self._normalize_available_models(
                data.available_models
            ),
            set_as_default=data.set_as_default,
            set_user_sdk_defaults=data.set_user_sdk_defaults,
            sdk_default_modes=data.sdk_default_modes,
            auto_provision_roles=auto_roles,
            model_override_conversation=self._normalize_default_model(
                data.model_override_conversation
            ),
            model_override_building=self._normalize_default_model(
                data.model_override_building
            ),
            expiry_notification_date=data.expiry_notification_date,
            managed_by_id=admin.id,
            created_at=now,
            updated_at=now,
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
        """
        parent = self._get_parent_or_404(session, managed_credential_id)

        # What this record claimed and pinned *before* the request, read
        # before a single field is written. Both are transitions rather than
        # states, and neither is recoverable once the new values are in:
        # ``previously_claimed`` scopes the conflict rule to slots this
        # request actually takes, and ``previous_overrides`` is what lets a
        # cleared override be told apart from one that was never set.
        previously_claimed = self._claimed_slots(
            parent.auto_provision_roles,
            parent.sdk_default_modes,
            parent.set_user_sdk_defaults,
        )
        previous_overrides = {
            mode: self._model_override_for(parent, mode)
            for mode in self._MODE_OVERRIDE_ATTR
        }

        # Validate the auto-provision shape against the *effective* values —
        # the submitted ones where given, the stored ones otherwise — before
        # any of them is written, so a PATCH that introduces a conflict cannot
        # half-apply. Only newly claimed slots can raise; see
        # ``_validate_auto_provision_uniqueness``.
        auto_roles = self._normalize_auto_provision_roles(
            data.auto_provision_roles
        )
        effective_roles = (
            auto_roles if auto_roles is not None else parent.auto_provision_roles
        )
        effective_modes = (
            data.sdk_default_modes
            if data.sdk_default_modes is not None
            else parent.sdk_default_modes
        )
        effective_sdk_defaults = (
            data.set_user_sdk_defaults
            if data.set_user_sdk_defaults is not None
            else parent.set_user_sdk_defaults
        )
        self._validate_auto_provision_uniqueness(
            session,
            parent.id,
            effective_roles or [],
            effective_modes or [],
            effective_sdk_defaults,
            previously_claimed=previously_claimed,
        )

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
        if auto_roles is not None:
            parent.auto_provision_roles = auto_roles
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
        cleared_overrides = {
            mode: previous
            for mode, previous in previous_overrides.items()
            if previous is not None
            and self._model_override_for(parent, mode) is None
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
    ) -> ManagedAICredentialReconcileResult:
        """Reconcile to empty membership (blast-radius gated) then delete the
        parent row. Returns the reconcile result so the route can surface any
        ``blocked`` members (409) when ``force`` is not set.

        When any member is blocked and ``force`` is False the parent row is left
        in place (delete is aborted)."""
        parent = self._get_parent_or_404(session, managed_credential_id)

        result = self.reconcile(
            session, admin, parent, [],
            apply_fields=False, force=force, key_rotated=False,
        )

        if result.blocked and not force:
            # Abort: leave the parent + remaining children intact.
            return result

        session.delete(parent)
        session.commit()
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

        Add-only. Nobody loses a credential here: the desired set is current
        members ∪ role matches, never a diff that could remove someone.

        ``dry_run`` returns the candidate list without writing anything, for
        the confirm dialog.
        """
        parent = self._get_parent_or_404(session, managed_credential_id)
        roles = parent.auto_provision_roles or []

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
        overwrites = self._count_default_overwrites(session, parent, matches)

        if dry_run:
            return ManagedAICredentialApplyResult(
                record=self._to_public(session, parent),
                dry_run=True,
                candidate_count=len(candidates),
                candidates=candidates,
                defaults_overwrite_count=overwrites,
            )

        result = self.add_members(
            session,
            parent=parent,
            user_ids=[c.user_id for c in candidates],
            actor=admin,
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
        # will not happen.
        modes: list[str] = []
        if parent.set_user_sdk_defaults and _TYPE_TO_SDK_ENGINE.get(parent.type):
            modes = [
                mode
                for mode in parent.sdk_default_modes
                if mode in self._MODE_POINTER_ATTR
            ]

        demoted_owner_ids: set[uuid.UUID] = set()
        if parent.set_as_default:
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
        """Set every child as its owner's default for the type + flag the parent
        ``set_as_default=True``."""
        parent = self._get_parent_or_404(session, managed_credential_id)
        members = self._current_members(session, parent)
        for owner_id, child in members.items():
            ai_credentials_service.set_default(session, child.id, owner_id)

        parent.set_as_default = True
        parent.updated_at = datetime.now(timezone.utc)
        session.add(parent)
        session.commit()
        session.refresh(parent)
        return self._to_public(session, parent)

    # ------------------------------------------------------------------ #
    # Listing / projection
    # ------------------------------------------------------------------ #

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
            # Keep only parents that have this user as a member.
            parent_ids = {
                row.managed_credential_id
                for row in session.exec(
                    select(AICredential).where(
                        AICredential.owner_id == target_user_id,
                        AICredential.managed_credential_id.is_not(None),
                    )
                ).all()
            }
            parents = [p for p in parents if p.id in parent_ids]

        return [self._to_public(session, p) for p in parents]

    def get(
        self,
        session: Session,
        admin: User,
        managed_credential_id: uuid.UUID,
    ) -> ManagedAICredentialPublic:
        parent = self._get_parent_or_404(session, managed_credential_id)
        return self._to_public(session, parent)

    def _to_public(
        self, session: Session, parent: ManagedAICredential
    ) -> ManagedAICredentialPublic:
        """Load children + owners and build the member projection."""
        children = session.exec(
            select(AICredential).where(
                AICredential.managed_credential_id == parent.id
            )
        ).all()

        # One query for every owner rather than ``session.get`` per child.
        # The list endpoint calls this once per parent, so the per-child
        # lookup was already an N+1 across the whole page; it matters more now
        # that the auto-provisioning path can reach a projection too.
        owner_ids = {child.owner_id for child in children}
        owners: dict[uuid.UUID, User] = {}
        if owner_ids:
            owners = {
                row.id: row
                for row in session.exec(
                    select(User).where(col(User.id).in_(owner_ids))
                ).all()
            }

        members: list[ManagedAICredentialMember] = []
        for child in children:
            owner = owners.get(child.owner_id)
            if owner is None:
                continue
            members.append(
                ManagedAICredentialMember(
                    user_id=owner.id,
                    email=owner.email,
                    full_name=owner.full_name,
                    child_credential_id=child.id,
                    is_default=child.is_default,
                )
            )

        # Derive is_oauth_token from the parent's stored key (anthropic OAuth).
        is_oauth = False
        if parent.type == AICredentialType.ANTHROPIC:
            try:
                api_key = self._decrypt_parent(parent).api_key or ""
                is_oauth = api_key.startswith("sk-ant-oat")
            except Exception:  # pragma: no cover - defensive
                is_oauth = False

        return ManagedAICredentialPublic(
            id=parent.id,
            name=parent.name,
            type=parent.type,
            base_url=parent.base_url,
            model=parent.model,
            default_model=parent.default_model,
            available_models=parent.available_models,
            set_as_default=parent.set_as_default,
            set_user_sdk_defaults=parent.set_user_sdk_defaults,
            sdk_default_modes=parent.sdk_default_modes,
            auto_provision_roles=parent.auto_provision_roles or [],
            model_override_conversation=parent.model_override_conversation,
            model_override_building=parent.model_override_building,
            expiry_notification_date=parent.expiry_notification_date,
            managed_by_id=parent.managed_by_id,
            has_api_key=True,
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
        return self._decrypt_parent(parent)


# Singleton instance (matches ai_credentials_service / admin_ai_credentials_service).
managed_ai_credentials_service = ManagedAICredentialsService()
