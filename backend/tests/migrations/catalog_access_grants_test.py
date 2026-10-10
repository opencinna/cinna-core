"""Revision 6ab98dfbbe62 (catalog_access_grants) on an isolated scratch database.

The migration rewrites rows no endpoint can create any more (old-shape grant
rows) and adds publisher ``maintain`` rows, so it is driven through Alembic with
raw SQL seeds, per ``tests/migrations/README.md``. Both grant tables are covered:
``bundle_access_grant`` and ``skill_package_access_grant``.
"""
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import settings

BEFORE = "08bfa6ee8d9f"
AFTER = "6ab98dfbbe62"


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
    name = f"app_migration_catalog_{AFTER}_{uuid.uuid4().hex[:10]}"
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


# ── raw seeds (old shape) ─────────────────────────────────────────────────


def _user(conn) -> uuid.UUID:
    uid = uuid.uuid4()
    conn.execute(
        text(
            'INSERT INTO "user" (id, email, is_active, is_superuser, task_sequence_counter) '
            "VALUES (:id, :email, true, false, 0)"
        ),
        {"id": uid, "email": f"{uid.hex}@example.com"},
    )
    return uid


def _bundle(conn, publisher: uuid.UUID | None) -> uuid.UUID:
    bid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO agent_bundle (id, bundle_id, display_name, publisher_user_id, "
            "created_at, updated_at) VALUES (:id, :slug, 'B', :pub, now(), now())"
        ),
        {"id": bid, "slug": f"io.example.{bid.hex[:12]}", "pub": publisher},
    )
    return bid


def _package(conn, publisher: uuid.UUID | None) -> uuid.UUID:
    pid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO skill_package (id, package_id, name, display_name, visibility, "
            "is_listed, publisher_user_id, created_at, updated_at) "
            "VALUES (:id, :pkg, :name, 'S', 'users', true, :pub, now(), now())"
        ),
        {"id": pid, "pkg": f"io.example.skill.{pid.hex[:12]}",
         "name": f"s-{pid.hex[:8]}", "pub": publisher},
    )
    return pid


def _old_grant(conn, table: str, fk: str, item: uuid.UUID, user: uuid.UUID, by: uuid.UUID | None) -> None:
    conn.execute(
        text(
            f"INSERT INTO {table} (id, {fk}, user_id, granted_by_user_id, created_at) "
            "VALUES (:id, :item, :user, :by, now())"
        ),
        {"id": uuid.uuid4(), "item": item, "user": user, "by": by},
    )


_KINDS = (
    ("bundle_access_grant", "bundle_id", _bundle),
    ("skill_package_access_grant", "package_id", _package),
)


def _rows(engine: Engine, table: str, fk: str, item: uuid.UUID, *, new_shape: bool) -> set[tuple]:
    cols = "user_id, granted_by_user_id"
    if new_shape:
        cols += ", principal_kind, principal_id, access"
    with engine.connect() as c:
        return {tuple(r) for r in c.execute(
            text(f"SELECT {cols} FROM {table} WHERE {fk} = :item"), {"item": item}
        )}


def _seed(engine: Engine) -> dict:
    """Per kind: a publisher without a row, a publisher who already holds a
    row (granted by someone else), and an ownerless item."""
    seeded: dict = {}
    with engine.begin() as conn:
        p1, p2, grantee, other = (_user(conn) for _ in range(4))
        seeded["users"] = (p1, p2, grantee, other)
        for table, fk, make in _KINDS:
            plain, preexisting, ownerless = make(conn, p1), make(conn, p2), make(conn, None)
            _old_grant(conn, table, fk, plain, grantee, p1)
            _old_grant(conn, table, fk, preexisting, p2, grantee)
            _old_grant(conn, table, fk, preexisting, other, p2)
            _old_grant(conn, table, fk, ownerless, grantee, None)
            seeded[table] = (plain, preexisting, ownerless)
    return seeded


# ── tests ─────────────────────────────────────────────────────────────────


def test_upgrade_turns_grants_into_user_use_and_publishers_into_maintainers(scratch) -> None:
    engine, url = scratch
    seeded = _seed(engine)
    p1, p2, grantee, other = seeded["users"]

    command.upgrade(_config(url), AFTER)

    for table, fk, _ in _KINDS:
        plain, preexisting, ownerless = seeded[table]
        # Old rows become user/use; the publisher gets a self-granted maintain row.
        assert _rows(engine, table, fk, plain, new_shape=True) == {
            (grantee, p1, "user", str(grantee), "use"),
            (p1, p1, "user", str(p1), "maintain"),
        }, table
        # A publisher who already had a row ends with ONE row, raised to maintain.
        assert _rows(engine, table, fk, preexisting, new_shape=True) == {
            (p2, grantee, "user", str(p2), "maintain"),
            (other, p2, "user", str(other), "use"),
        }, table
        # No publisher → no maintain row.
        assert _rows(engine, table, fk, ownerless, new_shape=True) == {
            (grantee, None, "user", str(grantee), "use"),
        }, table

        insp = inspect(engine)
        uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints(table)}
        assert uniques[f"uq_{'bundle' if fk == 'bundle_id' else 'skill'}_grant_principal"] == [
            fk, "principal_kind", "principal_id",
        ]
        assert not any(name.endswith("_user") for name in uniques), uniques
        cols = {c["name"]: c for c in insp.get_columns(table)}
        assert cols["user_id"]["nullable"] is True

        # A role row is now storable; a role row carrying a user_id is not.
        with engine.begin() as conn:
            conn.execute(text(
                f"INSERT INTO {table} (id, {fk}, principal_kind, principal_id, access, created_at) "
                "VALUES (:id, :item, 'role', 'agent-user', 'use', now())"
            ), {"id": uuid.uuid4(), "item": plain})
        with pytest.raises(Exception, match="ck_"):
            with engine.begin() as conn:
                conn.execute(text(
                    f"INSERT INTO {table} (id, {fk}, principal_kind, principal_id, user_id, access, created_at) "
                    "VALUES (:id, :item, 'role', 'admin', :user, 'use', now())"
                ), {"id": uuid.uuid4(), "item": plain, "user": other})


def test_downgrade_restores_the_old_rows(scratch) -> None:
    engine, url = scratch
    seeded = _seed(engine)
    p1, p2, grantee, other = seeded["users"]
    before = {
        table: {item: _rows(engine, table, fk, item, new_shape=False) for item in seeded[table]}
        for table, fk, _ in _KINDS
    }

    command.upgrade(_config(url), AFTER)
    # New-model data the downgrade must fold away: a role row, and a raised level.
    with engine.begin() as conn:
        for table, fk, _ in _KINDS:
            plain = seeded[table][0]
            conn.execute(text(
                f"INSERT INTO {table} (id, {fk}, principal_kind, principal_id, access, created_at) "
                "VALUES (:id, :item, 'role', 'agent-user', 'edit', now())"
            ), {"id": uuid.uuid4(), "item": plain})
            conn.execute(text(
                f"UPDATE {table} SET access = 'edit' WHERE {fk} = :item AND user_id = :user"
            ), {"item": plain, "user": grantee})

    command.downgrade(_config(url), BEFORE)

    for table, fk, _ in _KINDS:
        cols = {c["name"] for c in inspect(engine).get_columns(table)}
        assert not {"principal_kind", "principal_id", "access"} & cols
        assert not next(c for c in inspect(engine).get_columns(table) if c["name"] == "user_id")["nullable"]
        uniques = {u["name"] for u in inspect(engine).get_unique_constraints(table)}
        assert uniques & {"uq_bundle_grant_bundle_user", "uq_skill_grant_package_user"}
        for item in seeded[table]:
            assert _rows(engine, table, fk, item, new_shape=False) == before[table][item], (table, item)

    # And the revision upgrades again cleanly over the restored rows.
    command.upgrade(_config(url), AFTER)
    plain = seeded["bundle_access_grant"][0]
    assert _rows(engine, "bundle_access_grant", "bundle_id", plain, new_shape=True) == {
        (grantee, p1, "user", str(grantee), "use"),
        (p1, p1, "user", str(p1), "maintain"),
    }
