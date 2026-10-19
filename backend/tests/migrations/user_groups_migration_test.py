"""Revision 7896e6f51c03 (user_groups) on an isolated scratch database.

Covers upgrade/downgrade, the case-insensitive unique name, the membership
unique key, the DEFERRABLE SET NULL creator FK (creator who is also a member),
and ``ck_*_grant_group_fk`` on both catalog grant tables, per
``tests/migrations/README.md``.
"""
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import settings

BEFORE = "98e0bd120428"
AFTER = "7896e6f51c03"

NEW_TABLES = {
    "user_group",
    "user_group_member",
    "credential_group_share",
    "ai_credential_group_share",
    "artifact_group_grant",
    "ai_knowledge_git_repo_group_share",
}
GRANT_TABLES = (
    ("bundle_access_grant", "bundle_id", "bundle"),
    ("skill_package_access_grant", "package_id", "skill"),
)


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
    name = f"app_migration_groups_{AFTER}_{uuid.uuid4().hex[:10]}"
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


def _group(conn, name: str, creator: uuid.UUID | None = None) -> uuid.UUID:
    gid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO user_group (id, name, created_by_user_id, created_at, updated_at) "
            "VALUES (:id, :name, :by, now(), now())"
        ),
        {"id": gid, "name": name, "by": creator},
    )
    return gid


def _member(conn, group: uuid.UUID, user: uuid.UUID) -> None:
    conn.execute(
        text(
            "INSERT INTO user_group_member (id, group_id, user_id, created_at) "
            "VALUES (:id, :g, :u, now())"
        ),
        {"id": uuid.uuid4(), "g": group, "u": user},
    )


def _bundle(conn) -> uuid.UUID:
    bid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO agent_bundle (id, bundle_id, display_name, created_at, updated_at) "
            "VALUES (:id, :slug, 'B', now(), now())"
        ),
        {"id": bid, "slug": f"io.example.{bid.hex[:12]}"},
    )
    return bid


def _package(conn) -> uuid.UUID:
    pid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO skill_package (id, package_id, name, display_name, visibility, "
            "is_listed, created_at, updated_at) "
            "VALUES (:id, :pkg, :name, 'S', 'users', true, now(), now())"
        ),
        {"id": pid, "pkg": f"io.example.skill.{pid.hex[:12]}", "name": f"s-{pid.hex[:8]}"},
    )
    return pid


_MAKE_ITEM = {"bundle_access_grant": _bundle, "skill_package_access_grant": _package}


def _insert_grant(conn, table: str, fk: str, item, *, kind: str, principal_id: str,
                  group_id=None, user_id=None) -> None:
    conn.execute(
        text(
            f"INSERT INTO {table} (id, {fk}, principal_kind, principal_id, access, "
            "user_id, group_id, created_at) "
            "VALUES (:id, :item, :kind, :pid, 'use', :uid, :gid, now())"
        ),
        {"id": uuid.uuid4(), "item": item, "kind": kind, "pid": principal_id,
         "uid": user_id, "gid": group_id},
    )


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_upgrade_creates_tables_and_downgrade_removes_them(scratch) -> None:
    engine, url = scratch
    assert not NEW_TABLES & _tables(engine)

    command.upgrade(_config(url), AFTER)
    assert NEW_TABLES <= _tables(engine)
    insp = inspect(engine)
    for table, _, _ in GRANT_TABLES:
        assert "group_id" in {c["name"] for c in insp.get_columns(table)}
    assert "uq_user_group_name_lower" in {i["name"] for i in insp.get_indexes("user_group")}

    command.downgrade(_config(url), BEFORE)
    assert not NEW_TABLES & _tables(engine)
    insp = inspect(engine)
    for table, _, _ in GRANT_TABLES:
        assert "group_id" not in {c["name"] for c in insp.get_columns(table)}

    command.upgrade(_config(url), AFTER)
    assert NEW_TABLES <= _tables(engine)


def test_group_name_is_unique_case_insensitively_and_membership_is_unique(scratch) -> None:
    engine, url = scratch
    command.upgrade(_config(url), AFTER)
    with engine.begin() as conn:
        gid = _group(conn, "Engineering")
        uid = _user(conn)
        _member(conn, gid, uid)

    with pytest.raises(Exception, match="uq_user_group_name_lower"):
        with engine.begin() as conn:
            _group(conn, "ENGINEERING")
    with pytest.raises(Exception, match="uq_user_group_member"):
        with engine.begin() as conn:
            _member(conn, gid, uid)
    with engine.begin() as conn:
        _group(conn, "Engineering 2")  # a different name is fine


def test_deleting_a_user_who_created_and_belongs_to_a_group_keeps_the_group(scratch) -> None:
    engine, url = scratch
    command.upgrade(_config(url), AFTER)
    with engine.begin() as conn:
        creator, other = _user(conn), _user(conn)
        gid = _group(conn, "Ops", creator=creator)
        _member(conn, gid, creator)
        _member(conn, gid, other)

    with engine.begin() as conn:  # one statement reaches the group via two paths
        conn.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": creator})

    with engine.connect() as c:
        assert c.execute(
            text("SELECT created_by_user_id FROM user_group WHERE id = :g"), {"g": gid}
        ).scalar_one() is None
        assert c.execute(
            text("SELECT user_id FROM user_group_member WHERE group_id = :g"), {"g": gid}
        ).scalars().all() == [other]


@pytest.mark.parametrize("table,fk,prefix", GRANT_TABLES)
def test_group_grant_check_constraint_and_downgrade_drops_group_rows(
    scratch, table: str, fk: str, prefix: str
) -> None:
    engine, url = scratch
    command.upgrade(_config(url), AFTER)
    constraint = f"ck_{prefix}_grant_group_fk"
    with engine.begin() as conn:
        gid = _group(conn, "Readers")
        user = _user(conn)
        item = _MAKE_ITEM[table](conn)
        # Valid shapes: a group row, and a user row
        _insert_grant(conn, table, fk, item, kind="group", principal_id=str(gid), group_id=gid)
        _insert_grant(conn, table, fk, item, kind="user", principal_id=str(user), user_id=user)

    # group kind without group_id
    with pytest.raises(Exception, match=constraint):
        with engine.begin() as conn:
            _insert_grant(conn, table, fk, item, kind="group", principal_id=str(uuid.uuid4()))
    # group_id on a non-group row
    with pytest.raises(Exception, match=constraint):
        with engine.begin() as conn:
            _insert_grant(conn, table, fk, item, kind="role", principal_id="agent-user",
                          group_id=gid)

    # Deleting the group cascades its grant rows
    with engine.begin() as conn:
        gid2 = _group(conn, "Temp")
        _insert_grant(conn, table, fk, item, kind="group", principal_id=str(gid2), group_id=gid2)
        conn.execute(text("DELETE FROM user_group WHERE id = :g"), {"g": gid2})
        assert conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE group_id = :g"), {"g": gid2}
        ).scalar_one() == 0

    # Downgrade removes the group rows and keeps the user row
    command.downgrade(_config(url), BEFORE)
    with engine.connect() as c:
        kinds = c.execute(text(f"SELECT principal_kind FROM {table} WHERE {fk} = :i"),
                          {"i": item}).scalars().all()
    assert kinds == ["user"]
