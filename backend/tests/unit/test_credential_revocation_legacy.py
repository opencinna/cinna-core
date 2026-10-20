"""Repair shares left behind by the old generic sharing-disable path.

This state cannot be created through the current API. Use a stub session to
exercise its recovery; normal revocation and container cleanup are covered by
tests/api/credentials/test_credential_share_revocation.py.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.credentials.credential import (
    Credential,
    CredentialType,
    CredentialUpdate,
)
from app.models.credentials.credential_share import CredentialShare
from app.services.credentials.credential_share_service import CredentialShareService
from app.services.credentials.credentials_service import CredentialsService


@pytest.mark.parametrize("update_path", ["generic", "sharing"])
def test_disabling_again_revokes_legacy_shares(update_path, monkeypatch):
    owner_id, recipient_id = uuid.uuid4(), uuid.uuid4()
    credential = Credential(
        name="Legacy shared token",
        type=CredentialType.API_TOKEN,
        owner_id=owner_id,
        allow_sharing=False,
    )
    stale_share = CredentialShare(
        credential_id=credential.id,
        shared_with_user_id=recipient_id,
        shared_by_user_id=owner_id,
    )
    session = MagicMock()
    session.get.return_value = credential
    # Direct shares, then group shares (none for a legacy credential).
    session.exec.return_value.all.side_effect = [[stale_share], []]
    unlink = AsyncMock()
    monkeypatch.setattr(
        CredentialsService, "unlink_credential_from_revoked_recipients", unlink
    )
    monkeypatch.setattr(CredentialsService, "event_credential_updated", AsyncMock())

    if update_path == "generic":
        asyncio.run(
            CredentialsService.update_credential(
                session=session,
                credential_id=credential.id,
                owner_id=owner_id,
                credential_in=CredentialUpdate(allow_sharing=False),
            )
        )
    else:
        asyncio.run(
            CredentialShareService.update_credential_sharing(
                session=session,
                credential_id=credential.id,
                owner_id=owner_id,
                allow_sharing=False,
            )
        )

    session.delete.assert_called_once_with(stale_share)
    unlink.assert_awaited_once_with(
        session=session,
        credential_id=credential.id,
        recipient_user_ids=[recipient_id],
    )
    assert credential.allow_sharing is False
