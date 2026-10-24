"""Revision 48c3eee7b6cd (marketplace_access) on an isolated scratch database.

Marketplaces that exist before the revision have no access mode column; they
must read back ``allow_all`` (everything stays visible), per
``tests/migrations/README.md``. The new table and the four ``server_config``
policy columns are checked structurally.
"""
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import settings

BEFORE = "ab48f9755987"
AFTER = "48c3eee7b6cd"
POLICY_COLUMNS = {
    "addons_external_directories",
    "addons_custom_mcp",
    "addons_plugin_hooks",
    "addons_stdio_mcp",
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
    name = f"app_migration_mkt_access_{uuid.uuid4().hex[:10]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    created = False
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        command.upgrade(_config(url), BEFORE)
        yield engine, url
    finally:
        engine.dispose()
        if created:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _seed_marketplaces(engine: Engine) -> list[uuid.UUID]:
    owner = uuid.uuid4()
    ids = [uuid.uuid4(), uuid.uuid4()]
    with engine.begin() as conn:
        conn.execute(
            text(
                'INSERT INTO "user" (id, email, is_active, is_superuser, task_sequence_counter) '
                "VALUES (:id, :email, true, false, 0)"
            ),
            {"id": owner, "email": f"{owner.hex}@example.com"},
        )
        for mid in ids:
            conn.execute(
                text(
                    "INSERT INTO llm_plugin_marketplace (id, user_id, name, url, git_branch, "
                    "public_discovery, type, status, created_at, updated_at) "
                    "VALUES (:id, :user, :name, 'https://example.com/x.git', 'main', true, "
                    "'claude', 'active', now(), now())"
                ),
                {"id": mid, "user": owner, "name": f"m-{mid.hex[:8]}"},
            )
    return ids


def test_existing_marketplaces_read_back_allow_all_and_downgrade_is_clean(scratch) -> None:
    engine, url = scratch
    ids = _seed_marketplaces(engine)
    assert "access_mode" not in {c["name"] for c in inspect(engine).get_columns("llm_plugin_marketplace")}

    command.upgrade(_config(url), AFTER)

    with engine.connect() as conn:
        modes = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT id, access_mode FROM llm_plugin_marketplace"))
        }
    assert modes == {mid: "allow_all" for mid in ids}

    insp = inspect(engine)
    assert "llm_plugin_entry_access" in insp.get_table_names()
    assert {i["name"] for i in insp.get_indexes("llm_plugin_entry_access")} >= {
        "uq_llm_plugin_entry_access_entry"
    }
    assert POLICY_COLUMNS <= {c["name"] for c in insp.get_columns("server_config")}

    command.downgrade(_config(url), BEFORE)
    insp = inspect(engine)
    assert "llm_plugin_entry_access" not in insp.get_table_names()
    assert "access_mode" not in {c["name"] for c in insp.get_columns("llm_plugin_marketplace")}
    assert not POLICY_COLUMNS & {c["name"] for c in insp.get_columns("server_config")}
