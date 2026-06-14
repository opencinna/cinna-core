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
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.security import encrypt_field
from app.models.credentials.ai_credential import (
    AICredential,
    AICredentialCreate,
    AICredentialData,
    AICredentialType,
    AICredentialUpdate,
)
from app.models.credentials.managed_ai_credential import (
    ManagedAICredential,
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

    def _apply_sdk_defaults(
        self,
        session: Session,
        owner: User,
        child: AICredential,
        parent: ManagedAICredential,
    ) -> None:
        """Wire the owner's ``default_sdk_*`` + ``default_ai_credential_*_id`` for
        the parent's ``sdk_default_modes``. A mode whose composed engine is
        incompatible with the type is skipped (not a hard error)."""
        sdk_engine = _TYPE_TO_SDK_ENGINE.get(child.type)
        if not sdk_engine:
            return

        for mode in parent.sdk_default_modes:
            if mode not in ("conversation", "building"):
                continue
            if not is_credential_compatible_with_sdk(sdk_engine, child.type):
                continue
            if mode == "conversation":
                owner.default_sdk_conversation = sdk_engine
                owner.default_ai_credential_conversation_id = child.id
            else:
                owner.default_sdk_building = sdk_engine
                owner.default_ai_credential_building_id = child.id

        session.add(owner)
        session.commit()
        session.refresh(owner)

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

        # --- Post-commit wiring: best-effort, never demotes a created member. ---
        try:
            if parent.set_as_default:
                ai_credentials_service.set_default(session, child.id, owner.id)
                session.refresh(child)
            if parent.set_user_sdk_defaults:
                self._apply_sdk_defaults(session, owner, child, parent)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Child %s created for user %s under parent %s but post-create "
                "default/SDK wiring failed; member retained.",
                child.id, owner.id, parent.id,
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
        dirty = False
        if owner.default_ai_credential_conversation_id == child.id:
            owner.default_ai_credential_conversation_id = None
            owner.default_sdk_conversation = None
            dirty = True
        if owner.default_ai_credential_building_id == child.id:
            owner.default_ai_credential_building_id = None
            owner.default_sdk_building = None
            dirty = True
        if dirty:
            session.add(owner)
            session.commit()
            session.refresh(owner)

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
    ) -> ManagedAICredentialReconcileResult:
        """Diff desired-vs-actual membership and converge.

        - **Add** (desired − current): validate user exists/active (else
          ``skipped``); decrypt parent key; create child via the per-user
          pipeline; stamp markers + parent link; optional default / SDK defaults.
        - **Remove** (current − desired): delete child via the per-user pipeline
          (``admin_override=True``). On ``AICredentialInUseError`` append to
          ``blocked`` (member stays) unless ``force``.
        - **Update** (current ∩ desired, when ``apply_fields``): write parent
          scalar fields (and the rotated key when ``key_rotated``) through to the
          child; apply/clear default per ``set_as_default``.

        Per-child failures are collected into ``skipped``/``blocked``; the
        successful children are committed. Idempotent: identical desired set +
        unchanged fields → empty added/removed/updated.
        """
        desired = self._dedup(desired_user_ids)
        current = self._current_members(session, parent)
        current_ids = set(current.keys())
        desired_set = set(desired)

        added: list[ManagedAICredentialMember] = []
        removed: list[uuid.UUID] = []
        updated: list[ManagedAICredentialMember] = []
        skipped: list[ManagedReconcileSkip] = []
        blocked: list[ManagedReconcileBlock] = []

        key: AICredentialData | None = None

        # ----- Add (desired − current) -----
        to_add = [uid for uid in desired if uid not in current_ids]
        for owner_id in to_add:
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
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "Failed to provision child for user %s under parent %s",
                    owner_id, parent.id,
                )
                skipped.append(
                    ManagedReconcileSkip(
                        user_id=owner_id, reason="provision_failed"
                    )
                )
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

        # ----- Remove (current − desired) -----
        to_remove = [uid for uid in current_ids if uid not in desired_set]
        for owner_id in to_remove:
            child = current[owner_id]
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
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "Failed to remove child for user %s under parent %s",
                    owner_id, parent.id,
                )
                blocked.append(
                    ManagedReconcileBlock(
                        user_id=owner_id, reason="remove_failed", impact=None
                    )
                )
                continue
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
                    )
                except HTTPException:
                    raise
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "Failed to update child for user %s under parent %s",
                        owner_id, parent.id,
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

        return self.reconcile(
            session, admin, parent, desired,
            apply_fields=True, force=force, key_rotated=key_rotated,
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

        members: list[ManagedAICredentialMember] = []
        for child in children:
            owner = session.get(User, child.owner_id)
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
