"""Revisions 0e62eb9df5d4 (git_repo, git_repo_grant) and 08bfa6ee8d9f
(last pusher), on an isolated scratch database pinned to those revisions.

The behaviours (partial unique index, cascades, ``downgrade``) are database
facts, so this drives Alembic and seeds with raw SQL: the ORM models describe
the head schema, where ``git_repo`` is slim and ``git_repo_grant`` is gone
(``desktop_bundles``, C2). Access to desktop agents is covered through the API
in ``tests/api/catalog/``.
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.config import settings
from app.models import User

BEFORE = "49c748e2c50a"
AFTER = "0e62eb9df5d4"
#: ``git_repo.last_pushed_by_user_id``; the ORM-level tests need the model's full shape.
PUSHER = "08bfa6ee8d9f"


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
    name = f"app_migration_gitrepo_{AFTER}_{uuid.uuid4().hex[:10]}"
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


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _user(session: Session, role: str = "agent-user", **kw) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", role=role, **kw)
    session.add(user)
    session.commit()
    return user


def _repo(session: Session, owner: User | None, slug: str, kind: str = "agent") -> uuid.UUID:
    repo_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO git_repo (id, kind, slug, name, owner_user_id) "
            "VALUES (:id, :kind, :slug, :name, :owner)"
        ),
        {"id": repo_id, "kind": kind, "slug": slug, "name": slug.title(),
         "owner": owner.id if owner else None},
    )
    session.commit()
    return repo_id


def _grant(session: Session, repo_id: uuid.UUID, kind: str, pid: str, access: str,
           user: User | None = None) -> None:
    session.execute(
        text(
            "INSERT INTO git_repo_grant (id, repo_id, principal_kind, principal_id, user_id, access) "
            "VALUES (:id, :repo, :kind, :pid, :user, :access)"
        ),
        {"id": uuid.uuid4(), "repo": repo_id, "kind": kind, "pid": pid,
         "user": user.id if user else None, "access": access},
    )
    session.commit()


# ── migration ──────────────────────────────────────────────────────────────────


def test_upgrade_creates_tables_indexes_and_downgrade_removes_them(scratch) -> None:
    engine, url = scratch
    assert not {"git_repo", "git_repo_grant"} & _tables(engine)

    command.upgrade(_config(url), AFTER)
    assert {"git_repo", "git_repo_grant"} <= _tables(engine)
    insp = inspect(engine)
    repo_cols = {c["name"]: c for c in insp.get_columns("git_repo")}
    assert {"id", "kind", "slug", "name", "description", "owner_user_id", "marketplace_id",
            "default_branch", "head_sha", "manifest_cache", "created_at", "updated_at"} <= set(repo_cols)
    assert repo_cols["head_sha"]["nullable"] and not repo_cols["slug"]["nullable"]
    index = {i["name"]: i for i in insp.get_indexes("git_repo")}["uq_git_repo_agent_slug"]
    assert index["unique"] and index["column_names"] == ["slug"]
    uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints("git_repo_grant")}
    assert uniques["uq_git_repo_grant_principal"] == ["repo_id", "principal_kind", "principal_id"]
    grant_idx = {i["name"] for i in insp.get_indexes("git_repo_grant")}
    assert {"ix_git_repo_grant_repo_id", "ix_git_repo_grant_user_id"} <= grant_idx

    command.downgrade(_config(url), BEFORE)
    assert not {"git_repo", "git_repo_grant"} & _tables(engine)
    # and upgrades again cleanly
    command.upgrade(_config(url), AFTER)
    assert "git_repo" in _tables(engine)


def test_last_pusher_column_upgrade_and_downgrade(scratch) -> None:
    """Nullable FK to user (SET NULL); downgrade drops it and keeps the tables."""
    engine, url = scratch
    command.upgrade(_config(url), PUSHER)
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("git_repo")}
    assert cols["last_pushed_by_user_id"]["nullable"]
    fk = next(f for f in insp.get_foreign_keys("git_repo") if f["constrained_columns"] == ["last_pushed_by_user_id"])
    assert fk["referred_table"] == "user" and fk["options"].get("ondelete") == "SET NULL"

    command.downgrade(_config(url), AFTER)
    assert "last_pushed_by_user_id" not in {c["name"] for c in inspect(engine).get_columns("git_repo")}
    command.upgrade(_config(url), PUSHER)
    assert "last_pushed_by_user_id" in {c["name"] for c in inspect(engine).get_columns("git_repo")}


def test_slug_uniqueness_is_partial_and_grants_cascade(scratch) -> None:
    engine, url = scratch
    command.upgrade(_config(url), PUSHER)
    with Session(engine) as s:
        owner = _user(s)
        other = _user(s)
        repo = _repo(s, owner, "alpha")
        # same slug as another agent repo -> rejected
        with pytest.raises(IntegrityError):
            _repo(s, None, "alpha")
        s.rollback()
        # but a mirror may reuse an agent slug, and mirrors may repeat among themselves
        _repo(s, owner, "alpha", kind="mirror")
        _repo(s, owner, "alpha", kind="mirror")

        _grant(s, repo, "user", str(other.id), "read", other)
        _grant(s, repo, "everyone", "*", "read")
        # principal uniqueness per repo
        with pytest.raises(IntegrityError):
            _grant(s, repo, "everyone", "*", "write")
        s.rollback()

        repo_id, other_id, owner_id = repo, other.id, owner.id

    def count(sql: str, **p) -> int:
        with engine.connect() as c:
            return c.execute(text(sql), p).scalar_one()

    # deleting a user drops only that user's grants
    with engine.begin() as c:
        c.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": other_id})
    assert count("SELECT count(*) FROM git_repo_grant WHERE repo_id = :r", r=repo_id) == 1
    # deleting the owner keeps the repo (SET NULL)
    with engine.begin() as c:
        c.execute(text('DELETE FROM "user" WHERE id = :i'), {"i": owner_id})
    assert count("SELECT count(*) FROM git_repo WHERE id = :r AND owner_user_id IS NULL", r=repo_id) == 1
    # deleting the repo drops its grants
    with engine.begin() as c:
        c.execute(text("DELETE FROM git_repo WHERE id = :r"), {"r": repo_id})
    assert count("SELECT count(*) FROM git_repo_grant WHERE repo_id = :r", r=repo_id) == 0
