"""Migration ``49c748e2c50a`` — agent API issued key columns + data moves.

Lives in ``tests/infra/`` beside the other scratch-database tests (plan §12): a
migration has no API, runs once against rows no endpoint can create any more,
and fails by silently skipping rows. So this module seeds raw rows into a
scratch database of its own, drives Alembic directly and asserts on the data.

Never touches ``app_test`` (already at head; downgrading it under a running
suite corrupts every other test). The database is reset to ``BEFORE`` after
each test so the next ``upgrade`` really runs the migration.
"""
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.services.agent_api.agent_api_key_service import AgentApiKeyService

BEFORE = "e5a9c3d7f2b4"
AFTER = "49c748e2c50a"

SCRATCH_DB = "app_migration_issued_keys_test"

ISSUER = uuid.UUID("a0000000-0000-0000-0000-000000000001")
RECIPIENT = uuid.UUID("b0000000-0000-0000-0000-000000000001")
PRODUCER = uuid.UUID("c0000000-0000-0000-0000-000000000001")
RECIPIENT_AGENT = uuid.UUID("c0000000-0000-0000-0000-000000000002")
WORKSPACE = uuid.UUID("d0000000-0000-0000-0000-000000000001")

CRED_OTHER = uuid.UUID("e0000000-0000-0000-0000-000000000001")  # issued to RECIPIENT
CRED_SELF = uuid.UUID("e0000000-0000-0000-0000-000000000002")  # issued to ISSUER
CRED_CONN = uuid.UUID("e0000000-0000-0000-0000-000000000003")  # connection
TOK_OTHER = uuid.UUID("f0000000-0000-0000-0000-000000000001")
TOK_SELF = uuid.UUID("f0000000-0000-0000-0000-000000000002")
TOK_CONN = uuid.UUID("f0000000-0000-0000-0000-000000000003")

PRODUCER_NAME = "CRM  Agent (v2)"


@pytest.fixture(scope="session", autouse=True)
def setup_db() -> None:
    """Override root setup: this module migrates a scratch database only."""


def _config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def scratch_url() -> str:
    base = str(settings.TEST_SQLALCHEMY_DATABASE_URI).rsplit("/", 1)[0]
    assert SCRATCH_DB not in {"app", "app_test", "postgres"}
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    url = f"{base}/{SCRATCH_DB}"
    command.upgrade(_config(url), BEFORE)
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def scratch(scratch_url: str):
    engine = create_engine(scratch_url)
    yield engine
    engine.dispose()
    cfg = _config(scratch_url)
    # Back to BEFORE (with the seeded rows cleared so the NOT NULL restore in
    # the downgrade has nothing to trip on) for the next test.
    cleanup = create_engine(scratch_url)
    with cleanup.begin() as conn:
        for table in ("agent_credential_link", "credential_shares", "agent_api_token",
                      "credential", "agent", "user_workspace"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text('DELETE FROM "user" WHERE id IN (:a, :b)'), {"a": ISSUER, "b": RECIPIENT})
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    cleanup.dispose()
    if current != BEFORE:
        command.downgrade(cfg, BEFORE)


def _exec(engine, sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _rows(engine, sql: str, **params) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params).fetchall()]


def _seed(engine) -> None:
    for uid, email in ((ISSUER, "issuer@example.com"), (RECIPIENT, "recipient@example.com")):
        _exec(
            engine,
            """
            INSERT INTO "user" (
                id, email, is_active, is_superuser, task_sequence_counter, role,
                workspaces_enabled, two_factor_enabled, email_confirmed, conversation_style
            ) VALUES (:uid, :email, true, false, 0, 'agent-user', false, false, true, 'ai_default')
            """,
            uid=uid, email=email,
        )
    _exec(
        engine,
        "INSERT INTO user_workspace (id, user_id, name, created_at, updated_at) VALUES (:w, :u, 'ws', now(), now())",
        w=WORKSPACE, u=ISSUER,
    )
    for aid, owner, name in (
        (PRODUCER, ISSUER, PRODUCER_NAME),
        (RECIPIENT_AGENT, RECIPIENT, "Recipient Agent"),
    ):
        _exec(
            engine,
            """
            INSERT INTO agent (id, name, owner_id, show_on_dashboard, bundle_id,
                               agent_api_identity_enabled, agent_api_external_access_enabled)
            VALUES (:id, :name, :owner, false, 'b', false, true)
            """,
            id=aid, name=name, owner=owner,
        )
    for cid, name, owner, ws in (
        (CRED_OTHER, "issued-to-other", ISSUER, WORKSPACE),
        (CRED_SELF, "issued-to-self", ISSUER, WORKSPACE),
        (CRED_CONN, "connection", ISSUER, WORKSPACE),
    ):
        _exec(
            engine,
            """
            INSERT INTO credential (id, name, type, encrypted_data, owner_id, user_workspace_id)
            VALUES (:id, :name, 'AGENT_API', 'x', :owner, :ws)
            """,
            id=cid, name=name, owner=owner, ws=ws,
        )
    for tid, cid, kind, subject in (
        (TOK_OTHER, CRED_OTHER, "external", RECIPIENT),
        (TOK_SELF, CRED_SELF, "external", ISSUER),
        (TOK_CONN, CRED_CONN, "connection", None),
    ):
        _exec(
            engine,
            """
            INSERT INTO agent_api_token (id, agent_id, owner_id, credential_id, token_hash,
                token_prefix, read_only_override, is_active, created_at, updated_at, kind,
                subject_user_id)
            VALUES (:id, :agent, :owner, :cred, :hash, 'abcd1234', false, true, now(), now(),
                    :kind, :subject)
            """,
            id=tid, agent=PRODUCER, owner=ISSUER, cred=cid, hash=f"hash-{tid}",
            kind=kind, subject=subject,
        )
    # The issuer linked the other-issued key to their own agent (and to the
    # recipient's agent, which must survive); the self key stays linked.
    for aid, cid in ((PRODUCER, CRED_OTHER), (RECIPIENT_AGENT, CRED_OTHER), (PRODUCER, CRED_SELF)):
        _exec(engine, "INSERT INTO agent_credential_link (agent_id, credential_id) VALUES (:a, :c)",
              a=aid, c=cid)
    _exec(
        engine,
        """
        INSERT INTO credential_shares (id, credential_id, shared_with_user_id,
                                       shared_by_user_id, shared_at)
        VALUES (:id, :c, :w, :b, now())
        """,
        id=uuid.uuid4(), c=CRED_OTHER, w=RECIPIENT, b=ISSUER,
    )


def test_issued_to_other_key_moves_to_recipient_and_self_key_is_untouched(scratch, scratch_url):
    _seed(scratch)
    command.upgrade(_config(scratch_url), AFTER)

    creds = {
        r[0]: r[1:]
        for r in _rows(
            scratch,
            "SELECT id, owner_id, user_workspace_id, service_uri FROM credential",
        )
    }
    default_uri = AgentApiKeyService.default_service_uri(PRODUCER_NAME)

    # Issued to someone else: owner moves, workspace cleared, flag set.
    owner, workspace, service_uri = creds[CRED_OTHER]
    assert owner == RECIPIENT
    assert workspace is None
    assert service_uri == default_uri == "crm-agent-v2-api"

    flags = {
        r[0]: r[1]
        for r in _rows(scratch, "SELECT id, issuer_saw_value FROM agent_api_token")
    }
    assert flags[TOK_OTHER] is True

    # Issuer-side links removed; the recipient's own agent link survives.
    links = _rows(
        scratch,
        "SELECT agent_id FROM agent_credential_link WHERE credential_id = :c",
        c=CRED_OTHER,
    )
    assert links == [(RECIPIENT_AGENT,)]
    # Issuer-side shares of the moved credential are dropped.
    assert _rows(scratch, "SELECT 1 FROM credential_shares WHERE credential_id = :c", c=CRED_OTHER) == []

    # Self-issued key: owner / workspace / link untouched, flag false; only
    # the service_uri backfill applies and it equals the default.
    owner, workspace, service_uri = creds[CRED_SELF]
    assert owner == ISSUER
    assert workspace == WORKSPACE
    assert service_uri == default_uri
    assert flags[TOK_SELF] is False
    assert _rows(
        scratch,
        "SELECT agent_id FROM agent_credential_link WHERE credential_id = :c",
        c=CRED_SELF,
    ) == [(PRODUCER,)]

    # Connection credential: no backfill, no move.
    owner, workspace, service_uri = creds[CRED_CONN]
    assert owner == ISSUER and workspace == WORKSPACE and service_uri is None
    assert flags[TOK_CONN] is False


def test_agent_and_owner_foreign_keys_become_nullable_set_null(scratch, scratch_url):
    _seed(scratch)
    command.upgrade(_config(scratch_url), AFTER)

    nullable = {
        r[0]: r[1]
        for r in _rows(
            scratch,
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'agent_api_token' AND column_name IN ('agent_id', 'owner_id')",
        )
    }
    assert nullable == {"agent_id": "YES", "owner_id": "YES"}

    # Deleting the producer leaves the token row with a NULL agent_id.
    _exec(scratch, "DELETE FROM agent_credential_link WHERE agent_id = :a", a=PRODUCER)
    _exec(scratch, "DELETE FROM agent WHERE id = :a", a=PRODUCER)
    row = _rows(scratch, "SELECT agent_id, owner_id FROM agent_api_token WHERE id = :t", t=TOK_OTHER)
    assert row == [(None, ISSUER)]
