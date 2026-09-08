"""Where ``ManagedAICredentialsService._decrypt_parent`` reads the key from.

**A service-level test, not a pure unit** — see the ``db``-fixture exception in
this directory's README. It calls the service directly with the shared session
and no TestClient.

It exists because migration ``c23d6b59a8f5`` moved the key of every
auto-provisioning company credential onto its new ``ai_provider`` row and left
``managed_ai_credential.encrypted_data`` NULL. Nothing else in the suite can set
that shape up: no route creates a ``fixed_key`` provider until Phase 4 of the
ai-credential-providers plan. Without this test, ``_decrypt_parent``'s claim
that it follows the key to the provider is a docstring with nothing behind it —
and the failure it would hide is silent, because
``AccountProvisioningService`` swallows a provisioning error and still creates
the account, so a company key would simply stop reaching new hires.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.models.credentials.ai_credential import AICredentialType
from app.models.credentials.managed_ai_credential import ManagedAICredential
from app.models.credentials.provider_admin_credential import (
    AIProvider,
    AIProviderKind,
)
from app.services.credentials.managed_ai_credentials_service import (
    managed_ai_credentials_service as service,
)


def _provider(db: Session, kind: AIProviderKind, secret: str) -> AIProvider:
    now = datetime.now(timezone.utc)
    provider = AIProvider(
        id=uuid.uuid4(),
        name=f"provider-{kind.value}",
        kind=kind.value,
        provider_type=AICredentialType.ANTHROPIC,
        encrypted_secret=secret,
        config={},
        created_at=now,
        updated_at=now,
    )
    db.add(provider)
    db.commit()
    return provider


def _owned_credential(db: Session, provider: AIProvider) -> ManagedAICredential:
    """A provider-owned credential: no key of its own, exactly as the migration
    leaves one."""
    now = datetime.now(timezone.utc)
    parent = ManagedAICredential(
        id=uuid.uuid4(),
        name="owned",
        type=AICredentialType.ANTHROPIC,
        encrypted_data=None,
        provider_id=provider.id,
        created_at=now,
        updated_at=now,
    )
    db.add(parent)
    db.commit()
    return parent


def test_a_fixed_key_records_key_is_read_from_its_provider(db: Session) -> None:
    """The credential holds nothing; the provider holds the same envelope the
    credential used to, and the key comes back through it."""
    envelope = service._encrypt_key(
        AICredentialType.ANTHROPIC, "sk-ant-fixed-key", None, None
    )
    provider = _provider(db, AIProviderKind.FIXED_KEY, envelope)
    parent = _owned_credential(db, provider)

    assert parent.encrypted_data is None
    assert service._decrypt_parent(db, parent).api_key == "sk-ant-fixed-key"


def test_a_minted_records_key_is_refused_rather_than_invented(db: Session) -> None:
    """A minted provider's secret is an administration key, not a model key.
    Handing it back here would write it onto a member's child credential — and
    from there into the desktop client and every model call."""
    provider = _provider(db, AIProviderKind.MINTED, "an-admin-api-secret")
    parent = _owned_credential(db, provider)

    with pytest.raises(HTTPException) as raised:
        service._decrypt_parent(db, parent)
    assert raised.value.status_code == 400


def test_a_manual_records_key_still_comes_off_the_record(db: Session) -> None:
    """No provider, so nothing changed for it: the record is its own source."""
    now = datetime.now(timezone.utc)
    parent = ManagedAICredential(
        id=uuid.uuid4(),
        name="manual",
        type=AICredentialType.ANTHROPIC,
        encrypted_data=service._encrypt_key(
            AICredentialType.ANTHROPIC, "sk-ant-manual", None, None
        ),
        provider_id=None,
        created_at=now,
        updated_at=now,
    )
    db.add(parent)
    db.commit()

    assert service._decrypt_parent(db, parent).api_key == "sk-ant-manual"
