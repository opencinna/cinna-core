"""Value types shared between the managed-credential and key-provisioning services.

They live in their own module for one reason: ``key_provisioning_service`` needs
``ManagedAICredentialsService`` to materialise a child credential, and the
managed service needs to hand it revocations. Putting the plain values here keeps
that one real dependency pointing in a single direction, so the cycle is a
function-local import in exactly one place rather than two modules importing each
other at module level.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RevocationRequest:
    """Everything needed to destroy one minted key at the provider.

    A plain value, snapshotted **before** the rows it came from are deleted. The
    membership row carries the provider handles and is deleted by the removal
    that triggers the revoke, so reading them afterwards is not an option — and a
    revoke that cannot name its key is a key that stays live forever.
    """

    user_id: uuid.UUID
    parent_id: uuid.UUID
    provider_admin_credential_id: uuid.UUID | None
    provider_type: str
    external_key_ref: dict
    #: Whose security feed this revoke is recorded in — **not** always
    #: ``user_id``. A revoke usually belongs in the key holder's own feed, but
    #: ``security_event.user_id`` is NOT NULL with a foreign key to ``user``, and
    #: on the account-deletion path that row is gone by the time the provider is
    #: called. There the event goes to the administrator who deleted the account,
    #: which is also where it is useful.
    #:
    #: ``None`` means there is genuinely no feed to write to — a user deleting
    #: their own account is both the subject and the actor, and both are gone. The
    #: revoke still happens and is logged; it is simply not auditable to anyone,
    #: and pretending otherwise by inventing an owner would be worse.
    #:
    #: **Required, with no default**, for the same reason ``add_members``' actor
    #: is keyword-only with none: "there is nobody to tell" must be a decision at
    #: every construction site. It was a defaulted ``None`` for one commit, and in
    #: that commit the busiest revocation path of all — an admin removing a member
    #: — inherited the escape hatch meant for self-deletion and wrote no audit row
    #: at all.
    audit_user_id: uuid.UUID | None
