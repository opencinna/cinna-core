"""Revision 0e62eb9df5d4 (git_repo, git_repo_grant) and the library grant
evaluation that sits on those tables, on an isolated scratch database.

There is no HTTP surface for grants at this level of the build, and the
behaviours (partial unique index, cascades, ``downgrade``) are database facts,
so this drives Alembic and ``LibraryAccessService`` directly. The access matrix
through the API is covered by ``tests/api/library/library_api_test.py``.
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
from app.models.library.git_repo import (
    GitRepo,
    GitRepoGrant,
    LibraryGrantIn,
    LibraryPrincipalRole,
    LibraryPrincipalUser,
)
from app.services.library.exceptions import LibraryConflictError, LibraryValidationError
from app.services.library.library_access_service import LibraryAccessService as Access

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


def _repo(session: Session, owner: User | None, slug: str, kind: str = "agent") -> GitRepo:
    repo = GitRepo(kind=kind, slug=slug, name=slug.title(), owner_user_id=owner.id if owner else None)
    session.add(repo)
    session.commit()
    return repo


def _grant(session: Session, repo: GitRepo, kind: str, pid: str, access: str, user: User | None = None):
    row = GitRepoGrant(
        repo_id=repo.id, principal_kind=kind, principal_id=pid,
        user_id=user.id if user else None, access=access,
    )
    session.add(row)
    session.commit()
    return row


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
        s.add(GitRepo(kind="agent", slug="alpha", name="dup"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        # but a mirror may reuse an agent slug, and mirrors may repeat among themselves
        _repo(s, owner, "alpha", kind="mirror")
        _repo(s, owner, "alpha", kind="mirror")

        _grant(s, repo, "user", str(other.id), "read", other)
        _grant(s, repo, "everyone", "*", "read")
        # principal uniqueness per repo
        s.add(GitRepoGrant(repo_id=repo.id, principal_kind="everyone", principal_id="*", access="write"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()

        repo_id, other_id, owner_id = repo.id, other.id, owner.id

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


# ── LibraryAccessService on the real tables ────────────────────────────────────


def test_effective_access_takes_the_max_over_user_role_and_everyone(scratch) -> None:
    engine, url = scratch
    command.upgrade(_config(url), PUSHER)
    with Session(engine) as s:
        owner = _user(s, "agent-developer")
        dev = _user(s, "agent-developer")
        plain = _user(s, "agent-user")
        admin_role = _user(s, "admin")
        su = _user(s, "agent-user", is_superuser=True)
        gone = _user(s, "agent-user", is_active=False)
        repo = _repo(s, owner, "matrix")
        other = _repo(s, owner, "other")

        _grant(s, repo, "user", str(owner.id), "maintain", owner)
        _grant(s, repo, "role", "agent-developer", "write")
        _grant(s, repo, "everyone", "*", "read")
        _grant(s, other, "user", str(plain.id), "write", plain)
        _grant(s, other, "everyone", "*", "read")

        eff = lambda u, r=repo: Access.effective_access(s, r, u)  # noqa: E731
        assert eff(owner) == "maintain"  # user grant beats role + everyone
        assert eff(dev) == "write"  # role beats everyone
        assert eff(plain) == "read"  # everyone
        assert eff(gone) is None  # inactive user never has access
        assert eff(su) == "read"  # superuser: no automatic git access
        assert Access.effective_access(s, other, plain) == "write"

        # tighten: drop the everyone grant -> plain loses access
        for g in s.exec(__import__("sqlmodel").select(GitRepoGrant).where(
                GitRepoGrant.repo_id == repo.id, GitRepoGrant.principal_kind == "everyone")):
            s.delete(g)
        s.commit()
        assert eff(plain) is None
        assert eff(dev) == "write"
        # the admin role grant matches users with role=admin and any superuser
        _grant(s, repo, "role", "admin", "write")
        assert eff(admin_role) == "write" and eff(su) == "write"

        listed = {r.slug: a for r, a in Access.accessible_repos(s, dev)}
        assert listed == {"matrix": "write", "other": "read"}
        assert {r.slug for r, _ in Access.accessible_repos(s, plain)} == {"other"}
        assert Access.accessible_repos(s, gone) == []
        assert [r.slug for r, _ in Access.accessible_repos(s, dev, kind="mirror")] == []

        assert Access.can_manage(repo, owner, "maintain")
        assert not Access.can_manage(repo, dev, "write")
        assert Access.can_manage(repo, su, None)  # superuser manages without a grant

        assert Access.readers(s, repo) == {owner.id, dev.id, admin_role.id, su.id}
        assert gone.id not in Access.readers(s, other)


def test_replace_grants_validates_requires_a_maintainer_and_reports_readers(scratch) -> None:
    engine, url = scratch
    command.upgrade(_config(url), PUSHER)
    with Session(engine) as s:
        owner = _user(s, "agent-developer", full_name="Olga Owner")
        alice = _user(s)
        inactive = _user(s, is_active=False)
        repo = _repo(s, owner, "replace")
        _grant(s, repo, "user", str(owner.id), "maintain", owner)

        U = lambda u: LibraryPrincipalUser(user_id=u.id)  # noqa: E731
        before, after = Access.replace_grants(
            s, repo, [
                LibraryGrantIn(principal=U(owner), access="maintain"),
                LibraryGrantIn(principal=U(alice), access="read"),
            ], owner)
        assert before == {owner.id} and after == {owner.id, alice.id}
        assert [m.display_name for m in Access.maintainers(s, repo)] == ["Olga Owner"]

        # a result without a maintainer user grant is refused and changes nothing
        with pytest.raises(LibraryConflictError, match="at least one maintainer"):
            Access.replace_grants(s, repo, [
                LibraryGrantIn(principal="everyone", access="maintain"),
                LibraryGrantIn(principal=LibraryPrincipalRole(role="admin"), access="maintain"),
            ], owner)
        assert Access.readers(s, repo) == {owner.id, alice.id}

        keep = LibraryGrantIn(principal=U(owner), access="maintain")
        for bad in (
            [keep, LibraryGrantIn(principal=LibraryPrincipalRole(role="wizard"), access="read")],
            [keep, LibraryGrantIn(principal=U(inactive), access="read")],
            [keep, LibraryGrantIn(principal=LibraryPrincipalUser(user_id=uuid.uuid4()), access="read")],
            [keep, LibraryGrantIn(principal=U(alice), access="read"), LibraryGrantIn(principal=U(alice), access="write")],
            [keep, LibraryGrantIn(principal="everyone", access="read"), LibraryGrantIn(principal="everyone", access="write")],
        ):
            with pytest.raises(LibraryValidationError):
                Access.replace_grants(s, repo, bad, owner)
        assert Access.readers(s, repo) == {owner.id, alice.id}  # all failures were no-ops

        # widening to everyone: readers = every active user (not the inactive one)
        before, after = Access.replace_grants(
            s, repo, [keep, LibraryGrantIn(principal="everyone", access="read")], owner)
        assert before == {owner.id, alice.id}
        assert inactive.id not in after and {owner.id, alice.id} <= after

        # role grants resolve to that role's active users
        _, after = Access.replace_grants(
            s, repo, [keep, LibraryGrantIn(principal=LibraryPrincipalRole(role="agent-user"), access="read")], owner)
        assert alice.id in after and inactive.id not in after

        # an inactive maintainer is not listed
        _, _ = Access.replace_grants(
            s, repo, [keep, LibraryGrantIn(principal=U(alice), access="maintain")], owner)
        alice.is_active = False
        s.add(alice)
        s.commit()
        assert [m.user_id for m in Access.maintainers(s, repo)] == [owner.id]
