"""
Helpers for creating AgentEnvironment test fixtures with scoped env tokens.

These are documented DB-seam helpers (like ``tests/utils/environment.py``):
there is no public API endpoint that mints a scoped agent-environment JWT, so
the environment record and token are created directly on the test session.

The token format EXACTLY mirrors what ``EnvironmentLifecycleManager._generate_auth_token``
produces in production (same claims, same ``create_access_token`` call) — tests
using this helper exercise the real ``AgentEnvContextDep`` code path.

All writes use ``db.flush()`` (NOT ``db.commit()``) to stay inside the test
transaction's savepoint — changes are rolled back after the test.

Exemption note
--------------
Rule 1 of ``backend/tests/README.md`` bans imports from ``app.core.security``
in ``tests/api/`` files. This utility lives in ``tests/utils/`` (not
``tests/api/``), follows the same exemption documented for
``tests/utils/platform_token.py`` and ``tests/utils/environment.py``, and is
the ONLY place in the test suite where an env token is minted directly.
"""

import hashlib
import uuid
from datetime import timedelta

from sqlmodel import Session

from app.core import security
from app.core.config import settings
from app.models import AgentEnvironment


def create_env_with_token(
    db: Session,
    agent_id: str | uuid.UUID,
    owner_id: str | uuid.UUID,
    *,
    env_name: str | None = None,
    status: str = "running",
) -> tuple[AgentEnvironment, dict[str, str]]:
    """Create an AgentEnvironment with a scoped new-format env JWT.

    Exactly mirrors ``EnvironmentLifecycleManager._generate_auth_token``:
    - ``sub = str(owner_id)``
    - ``extra_claims = {token_type: "agent_env", aud: "agent_env", env_id: ..., agent_id: ...}``
    - ``expires_delta = AGENT_ENV_TOKEN_EXPIRE_DAYS`` days

    Also sets ``env.auth_token_hash`` (SHA-256 of the token) so that the full
    hash-verification path in ``AgentEnvContextDep`` is exercised.

    Args:
        db:       The test database session (function-scoped; changes are
                  visible to the TestClient but rolled back after the test).
        agent_id: UUID of the agent this environment belongs to. Must already
                  exist in the test DB (create via ``create_agent_via_api`` first).
        owner_id: UUID of the agent's owner. Used as ``sub`` in the JWT.
        env_name: Optional environment template name. Defaults to
                  ``settings.DEFAULT_AGENT_ENV_NAME``.
        status:   Environment status string (default ``"running"``).

    Returns:
        (env, headers) where:
          - ``env`` is the newly-flushed ``AgentEnvironment`` DB row.
          - ``headers`` is a dict with ``Authorization: Bearer <token>`` and
            ``X-Agent-Env-Id: <env_id>``, ready to pass to ``client.post(...)``
            or as ``auth_headers`` to ``ScriptedAgentEnvConnector``.
    """
    if isinstance(agent_id, str):
        agent_id = uuid.UUID(agent_id)
    if isinstance(owner_id, str):
        owner_id = uuid.UUID(owner_id)

    env_name = env_name or settings.DEFAULT_AGENT_ENV_NAME

    # Create the environment row first so we have its id for the token claims.
    env = AgentEnvironment(
        agent_id=agent_id,
        env_name=env_name,
        status=status,
        is_active=True,
        config={},
    )
    db.add(env)
    db.flush()  # assigns env.id without committing the outer savepoint

    # Mint the scoped JWT — mirrors _generate_auth_token exactly.
    token = security.create_access_token(
        subject=str(owner_id),
        expires_delta=timedelta(days=settings.AGENT_ENV_TOKEN_EXPIRE_DAYS),
        extra_claims={
            "token_type": "agent_env",
            "aud": "agent_env",
            "env_id": str(env.id),
            "agent_id": str(agent_id),
        },
    )

    # Store the raw token in config (legacy verbatim-compare fallback) AND set
    # auth_token_hash so the authoritative hash-verification path fires.
    env.config = {"auth_token": token}
    env.auth_token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.add(env)
    db.flush()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Env-Id": str(env.id),
    }
    return env, headers
