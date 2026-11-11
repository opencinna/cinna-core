"""Voice transcription storage migration (46e56c924dd3) on a scratch database.

Never touches app_test. Covers: new server_config columns default off for an
existing row, the usage ledger's constraints and cascades, the provider FK's
ON DELETE SET NULL (never RESTRICT), and a clean downgrade.
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings

BEFORE = "f725f8313224"
AFTER = "46e56c924dd3"
NEW_COLUMNS = {
    "voice_transcription_enabled",
    "voice_transcription_provider_id",
    "voice_transcription_monthly_minutes_per_user",
}


@pytest.fixture(scope="session", autouse=True)
def setup_db() -> None:
    """Override root setup: this module neither migrates nor seeds app_test."""


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture
def scratch() -> Iterator[tuple[Engine, str]]:
    base = make_url(str(settings.TEST_SQLALCHEMY_DATABASE_URI))
    name = f"app_migration_voice_46e56c924dd3_{uuid.uuid4().hex[:10]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch_url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(scratch_url)
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        command.upgrade(_config(scratch_url), BEFORE)
        yield engine, scratch_url
    finally:
        engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _execute(engine: Engine, sql: str, **params: object) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def _rows(engine: Engine, sql: str) -> list[tuple]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql))]


def _seed_user(engine: Engine) -> uuid.UUID:
    user_id = uuid.uuid4()
    _execute(
        engine,
        """INSERT INTO "user" (
            id, email, is_active, is_superuser, role, workspaces_enabled,
            task_sequence_counter, two_factor_enabled, email_confirmed, conversation_style
        ) VALUES (:id, :email, true, false, 'agent-developer', false, 0, false, true, 'ai_default')""",
        id=user_id,
        email=f"{user_id}@migration.example",
    )
    return user_id


def _usage(engine: Engine, user_id: uuid.UUID, session_id: uuid.UUID, provider_id: uuid.UUID | None = None) -> None:
    _execute(
        engine,
        """INSERT INTO voice_transcription_usage (id, user_id, session_id, started_at, ended_at,
            audio_seconds, replay_seconds, provider_id, client_kind, outcome)
        VALUES (:id, :user, :session, NOW(), NOW(), 12.5, 0, :provider, 'desktop', 'stop')""",
        id=uuid.uuid4(),
        user=user_id,
        session=session_id,
        provider=provider_id,
    )


def test_upgrade_adds_settings_defaults_off_and_usage_ledger(scratch: tuple[Engine, str]) -> None:
    engine, url = scratch
    before_cols = {c["name"] for c in inspect(engine).get_columns("server_config")}
    assert not NEW_COLUMNS & before_cols
    assert "voice_transcription_usage" not in inspect(engine).get_table_names()
    # An existing server_config row must survive and come out with the feature off.
    if not _rows(engine, "SELECT 1 FROM server_config"):
        _execute(engine, "INSERT INTO server_config (id, disclaimer_enabled, disclaimer_markdown, "
            "disclaimer_display_mode, disclaimer_version, updated_at) "
            "VALUES (:id, false, '', 'once', 1, NOW())", id=uuid.uuid4())
    count_before = len(_rows(engine, "SELECT 1 FROM server_config"))

    command.upgrade(_config(url), AFTER)

    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("server_config")}
    assert NEW_COLUMNS <= set(columns)
    assert columns["voice_transcription_enabled"]["nullable"] is False
    assert columns["voice_transcription_provider_id"]["nullable"] is True
    assert columns["voice_transcription_monthly_minutes_per_user"]["nullable"] is True
    rows = _rows(
        engine,
        "SELECT voice_transcription_enabled, voice_transcription_provider_id, "
        "voice_transcription_monthly_minutes_per_user FROM server_config",
    )
    assert len(rows) == count_before
    assert all(row == (False, None, None) for row in rows)

    usage = {c["name"]: c for c in inspector.get_columns("voice_transcription_usage")}
    assert {
        "id", "user_id", "session_id", "started_at", "ended_at", "audio_seconds",
        "replay_seconds", "provider_id", "provider_type", "model", "client_kind", "outcome",
    } == set(usage)
    assert usage["provider_id"]["nullable"] is True
    assert usage["started_at"]["type"].timezone is True
    assert usage["ended_at"]["type"].timezone is True
    indexes = {i["name"]: i for i in inspector.get_indexes("voice_transcription_usage")}
    assert indexes["ix_voice_transcription_usage_user_started"]["column_names"] == ["user_id", "started_at"]
    uniques = inspector.get_unique_constraints("voice_transcription_usage")
    assert any(u["column_names"] == ["session_id"] for u in uniques)


def test_usage_ledger_constraints_and_cascades(scratch: tuple[Engine, str]) -> None:
    engine, url = scratch
    command.upgrade(_config(url), AFTER)
    user = _seed_user(engine)
    session_id = uuid.uuid4()
    _usage(engine, user, session_id)
    with pytest.raises(IntegrityError):  # one row per session
        _usage(engine, user, session_id)
    with pytest.raises(IntegrityError):  # unknown user
        _usage(engine, uuid.uuid4(), uuid.uuid4())
    with pytest.raises(IntegrityError):  # unknown provider
        _usage(engine, user, uuid.uuid4(), provider_id=uuid.uuid4())
    # Deleting the user takes the ledger rows with it.
    _execute(engine, 'DELETE FROM "user" WHERE id = :id', id=user)
    assert _rows(engine, "SELECT id FROM voice_transcription_usage") == []


def test_provider_foreign_keys_set_null_on_delete_never_restrict(scratch: tuple[Engine, str]) -> None:
    engine, url = scratch
    command.upgrade(_config(url), AFTER)
    inspector = inspect(engine)
    fks = {
        fk["name"]: fk for fk in inspector.get_foreign_keys("server_config")
    }
    fk = fks["fk_server_config_voice_transcription_provider_id"]
    assert fk["referred_table"] == "ai_provider"
    assert fk["constrained_columns"] == ["voice_transcription_provider_id"]
    assert fk["options"].get("ondelete") == "SET NULL"
    usage_fk = next(
        f for f in inspector.get_foreign_keys("voice_transcription_usage")
        if f["constrained_columns"] == ["provider_id"]
    )
    assert usage_fk["options"].get("ondelete") == "SET NULL"
    user_fk = next(
        f for f in inspector.get_foreign_keys("voice_transcription_usage")
        if f["constrained_columns"] == ["user_id"]
    )
    assert user_fk["options"].get("ondelete") == "CASCADE"


def test_downgrade_removes_everything_and_keeps_server_config_rows(scratch: tuple[Engine, str]) -> None:
    engine, url = scratch
    command.upgrade(_config(url), AFTER)
    command.downgrade(_config(url), BEFORE)
    inspector = inspect(engine)
    assert "voice_transcription_usage" not in inspector.get_table_names()
    assert not NEW_COLUMNS & {c["name"] for c in inspector.get_columns("server_config")}
    assert not any(
        fk["name"] == "fk_server_config_voice_transcription_provider_id"
        for fk in inspector.get_foreign_keys("server_config")
    )
    # Round trip: upgrading again works on the downgraded schema.
    command.upgrade(_config(url), AFTER)
    assert NEW_COLUMNS <= {c["name"] for c in inspect(engine).get_columns("server_config")}
