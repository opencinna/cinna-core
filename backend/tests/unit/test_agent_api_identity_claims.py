"""Identity token claims: mint/verify round trip with and without ``agent_id``."""
import uuid
from datetime import timedelta

from app.core import security
from app.services.agent_api.agent_api_identity_service import (
    IDENTITY_AUDIENCE,
    IDENTITY_TYPE,
    AgentApiIdentityService,
)


def test_round_trip_with_agent_id() -> None:
    user_id, agent_id = uuid.uuid4(), uuid.uuid4()
    token = AgentApiIdentityService.mint(user_id, agent_id)
    claims = AgentApiIdentityService.verify_claims(token)
    assert claims is not None
    assert claims.user_id == user_id
    assert claims.agent_id == agent_id
    assert AgentApiIdentityService.verify(token) == user_id


def test_round_trip_without_agent_id() -> None:
    user_id = uuid.uuid4()
    claims = AgentApiIdentityService.verify_claims(AgentApiIdentityService.mint(user_id))
    assert claims is not None
    assert claims.user_id == user_id
    assert claims.agent_id is None


def test_old_token_without_claim_still_verifies() -> None:
    user_id = uuid.uuid4()
    old = security.create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(days=1),
        extra_claims={"aud": IDENTITY_AUDIENCE, "type": IDENTITY_TYPE},
    )
    claims = AgentApiIdentityService.verify_claims(old)
    assert claims is not None and claims.user_id == user_id and claims.agent_id is None


def test_malformed_agent_id_claim_is_dropped_not_fatal() -> None:
    user_id = uuid.uuid4()
    token = security.create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(days=1),
        extra_claims={"aud": IDENTITY_AUDIENCE, "type": IDENTITY_TYPE, "agent_id": "not-a-uuid"},
    )
    claims = AgentApiIdentityService.verify_claims(token)
    assert claims is not None
    assert claims.user_id == user_id
    assert claims.agent_id is None


def test_invalid_tokens_yield_none() -> None:
    assert AgentApiIdentityService.verify_claims(None) is None
    assert AgentApiIdentityService.verify_claims("") is None
    assert AgentApiIdentityService.verify_claims("garbage") is None
    wrong_aud = security.create_access_token(
        subject=str(uuid.uuid4()),
        expires_delta=timedelta(days=1),
        extra_claims={"aud": "something_else", "type": IDENTITY_TYPE},
    )
    assert AgentApiIdentityService.verify_claims(wrong_aud) is None
