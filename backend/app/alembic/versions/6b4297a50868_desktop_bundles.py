"""desktop_bundles: desktop agents become catalog bundles

``agent_bundle`` gains ``runs_on`` (``cloud``/``desktop``), ``git_repo_id``
(1:1, RESTRICT), ``manifest_id`` and ``slug`` (both unique among desktop
bundles) and the check ``ck_agent_bundle_desktop_fields``.
``agent_bundle_revision`` gains ``git_commit_sha``; ``snapshot_path`` becomes
nullable (a ``share`` revision has no snapshot on disk).

Each ``git_repo(kind='agent')`` is converted, oldest first, into an
``AgentBundle(runs_on='desktop')``:

- ``manifest_id`` = ``manifest_cache.id`` when it is a UUID no earlier repo
  took, else a fresh uuid4 (logged; that repo refuses pushes until the desktop
  shares it as a new agent, since the push policy binds the stored id);
- ``bundle_id`` minted by ``BundleIdService.desktop_bundle_id`` (as the create
  route does), clear of every ``agent_bundle.bundle_id`` and ``agent.bundle_id``;
- visibility ``public`` for an ``everyone`` grant, else ``users`` when any
  grant other than the owner's exists, else ``private``;
- grants copied to ``bundle_access_grant`` (``read→use``, ``write→edit``,
  ``maintain→maintain``; ``everyone`` dropped), the owner upserted to
  ``maintain``;
- revision 1 (``origin='share'``) only for a repo that has been pushed to.

Then ``git_repo_grant`` is dropped, and so are ``git_repo``'s ``slug``,
``name``, ``description`` and ``owner_user_id``.

Downgrade is LOSSY: the ``git_repo`` columns and ``git_repo_grant`` are
restored from the desktop bundles (``use→read``, ``edit→write``, and an
``everyone/read`` row for a public one), then every desktop bundle is deleted
with its revisions and grants — revision history, ``bundle_id`` and
``is_listed`` are lost.

Revision ID: 6b4297a50868
Revises: 6ab98dfbbe62
Create Date: 2026-10-10 12:00:00.000000

"""
import json
import logging
import uuid

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '6b4297a50868'
down_revision = '6ab98dfbbe62'
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_LEVEL_UP = {"read": "use", "write": "edit", "maintain": "maintain"}


def _minting():
    """``(prefix, mint)`` from the service the create route uses. The import is
    deliberately local: it pulls only ``settings``, never the ORM models."""
    from app.services.bundles.bundle_id_service import BundleIdService

    return BundleIdService.reversed_host_prefix(), BundleIdService.desktop_bundle_id


def upgrade():
    # 1. Columns, indexes, check.
    op.add_column('agent_bundle', sa.Column(
        'runs_on', sa.String(length=16), server_default=sa.text("'cloud'"), nullable=False,
    ))
    op.add_column('agent_bundle', sa.Column('git_repo_id', sa.Uuid(), nullable=True))
    op.add_column('agent_bundle', sa.Column('manifest_id', sa.Uuid(), nullable=True))
    op.add_column('agent_bundle', sa.Column(
        'slug', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True,
    ))
    op.create_foreign_key(
        'agent_bundle_git_repo_id_fkey', 'agent_bundle', 'git_repo',
        ['git_repo_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_unique_constraint('agent_bundle_git_repo_id_key', 'agent_bundle', ['git_repo_id'])
    op.create_index(
        'uq_agent_bundle_desktop_manifest_id', 'agent_bundle', ['manifest_id'],
        unique=True, postgresql_where=sa.text("runs_on = 'desktop'"),
    )
    op.create_index(
        'uq_agent_bundle_desktop_slug', 'agent_bundle', ['slug'],
        unique=True, postgresql_where=sa.text("runs_on = 'desktop'"),
    )
    op.create_check_constraint(
        'ck_agent_bundle_desktop_fields', 'agent_bundle',
        "runs_on = 'cloud' OR (git_repo_id IS NOT NULL AND manifest_id IS NOT NULL "
        "AND slug IS NOT NULL)",
    )
    op.add_column('agent_bundle_revision', sa.Column(
        'git_commit_sha', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=True,
    ))
    op.alter_column(
        'agent_bundle_revision', 'snapshot_path',
        existing_type=sa.String(length=1024), nullable=True,
    )

    # 2. Convert every agent repo.
    _convert_repos(op.get_bind())

    # 3. The library's own access model and metadata go.
    op.drop_index('ix_git_repo_grant_user_id', table_name='git_repo_grant')
    op.drop_index('ix_git_repo_grant_repo_id', table_name='git_repo_grant')
    op.drop_table('git_repo_grant')
    op.drop_index(
        'uq_git_repo_agent_slug', table_name='git_repo',
        postgresql_where=sa.text("kind = 'agent'"),
    )
    op.drop_index('ix_git_repo_owner_user_id', table_name='git_repo')
    op.drop_column('git_repo', 'owner_user_id')
    op.drop_column('git_repo', 'description')
    op.drop_column('git_repo', 'name')
    op.drop_column('git_repo', 'slug')


def _convert_repos(bind) -> None:
    prefix, mint = _minting()
    taken = {row[0] for row in bind.execute(sa.text(
        "SELECT bundle_id FROM agent_bundle UNION SELECT bundle_id FROM agent"
    ))}
    used_manifest_ids: set[uuid.UUID] = set()
    repos = bind.execute(sa.text(
        "SELECT id, slug, name, description, owner_user_id, head_sha, "
        "last_pushed_by_user_id, manifest_cache, created_at, updated_at "
        "FROM git_repo WHERE kind = 'agent' ORDER BY created_at, id"
    )).mappings().all()

    for repo in repos:
        manifest = repo["manifest_cache"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        manifest_id = _manifest_id(repo["id"], manifest, used_manifest_ids)
        used_manifest_ids.add(manifest_id)
        bundle_id = mint(prefix, manifest_id, taken)
        taken.add(bundle_id)

        grants = bind.execute(sa.text(
            "SELECT principal_kind, principal_id, user_id, access, granted_by_user_id, created_at "
            "FROM git_repo_grant WHERE repo_id = :repo_id"
        ), {"repo_id": repo["id"]}).mappings().all()
        owner_id = repo["owner_user_id"]
        if any(g["principal_kind"] == "everyone" for g in grants):
            visibility = "public"
        elif any(
            not (g["principal_kind"] == "user" and owner_id is not None and g["user_id"] == owner_id)
            for g in grants
        ):
            visibility = "users"
        else:
            visibility = "private"

        bundle_uuid = uuid.uuid4()
        bind.execute(sa.text(
            "INSERT INTO agent_bundle (id, bundle_id, display_name, description, "
            "publisher_user_id, is_listed, visibility, default_install_mode, "
            "created_at, updated_at, runs_on, git_repo_id, manifest_id, slug) VALUES "
            "(:id, :bundle_id, :display_name, :description, :publisher, true, :visibility, "
            "'manual', :created_at, :updated_at, 'desktop', :git_repo_id, :manifest_id, :slug)"
        ), {
            "id": bundle_uuid,
            "bundle_id": bundle_id,
            "display_name": repo["name"],
            "description": repo["description"],
            "publisher": owner_id,
            "visibility": visibility,
            "created_at": repo["created_at"],
            "updated_at": repo["updated_at"],
            "git_repo_id": repo["id"],
            "manifest_id": manifest_id,
            "slug": repo["slug"],
        })

        has_maintainer = False
        owner_granted = False
        for grant in grants:
            if grant["principal_kind"] not in ("user", "role"):
                continue  # ``everyone`` is carried by visibility now
            access = _LEVEL_UP.get(grant["access"])
            if access is None:
                continue
            if grant["principal_kind"] == "user" and owner_id is not None and grant["user_id"] == owner_id:
                access = "maintain"
                owner_granted = True
            if access == "maintain" and grant["principal_kind"] == "user":
                has_maintainer = True
            _insert_grant(bind, bundle_uuid, grant["principal_kind"], grant["principal_id"],
                          grant["user_id"], access, grant["granted_by_user_id"], grant["created_at"])
        if owner_id is not None and not owner_granted:
            _insert_grant(bind, bundle_uuid, "user", str(owner_id), owner_id, "maintain",
                          owner_id, repo["created_at"])
            has_maintainer = True
        if not has_maintainer:
            logger.warning(
                "catalog_desktop_bundles: repo %s has no maintainer and no owner; "
                "a superuser can manage it", repo["id"],
            )

        if repo["head_sha"]:
            revision_id = uuid.uuid4()
            bind.execute(sa.text(
                "INSERT INTO agent_bundle_revision (id, bundle_id, revision_number, origin, "
                "manifest, required_credential_specs, schedules, plugin_specs, snapshot_path, "
                "content_hash, git_commit_sha, published_by_user_id, published_at, release_notes) "
                "VALUES (:id, :bundle_id, 1, 'share', CAST(:manifest AS json), "
                "CAST('[]' AS json), CAST('[]' AS json), CAST('[]' AS json), NULL, :sha, :sha, "
                ":published_by, :published_at, NULL)"
            ), {
                "id": revision_id,
                "bundle_id": bundle_uuid,
                "manifest": json.dumps(manifest or {}),
                "sha": repo["head_sha"],
                "published_by": repo["last_pushed_by_user_id"],
                "published_at": repo["updated_at"],
            })
            bind.execute(sa.text(
                "UPDATE agent_bundle SET latest_revision_id = :rev WHERE id = :id"
            ), {"rev": revision_id, "id": bundle_uuid})


def _manifest_id(repo_id, manifest, used: set[uuid.UUID]) -> uuid.UUID:
    raw = manifest.get("id") if isinstance(manifest, dict) else None
    reason = "missing"
    if isinstance(raw, str):
        try:
            parsed = uuid.UUID(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed not in used:
                return parsed
            reason = "duplicate"
    fresh = uuid.uuid4()
    logger.warning(
        "catalog_desktop_bundles: repo %s manifest id %s → %s", repo_id, reason, fresh
    )
    return fresh


def _insert_grant(bind, bundle_uuid, kind, principal_id, user_id, access, granted_by, created_at):
    bind.execute(sa.text(
        "INSERT INTO bundle_access_grant (id, bundle_id, principal_kind, principal_id, user_id, "
        "access, granted_by_user_id, created_at) VALUES (:id, :bundle_id, :kind, :principal_id, "
        ":user_id, :access, :granted_by, :created_at)"
    ), {
        "id": uuid.uuid4(),
        "bundle_id": bundle_uuid,
        "kind": kind,
        "principal_id": principal_id,
        "user_id": user_id,
        "access": access,
        "granted_by": granted_by,
        "created_at": created_at,
    })


def downgrade():
    # 1. git_repo metadata back, from each desktop bundle.
    op.add_column('git_repo', sa.Column('slug', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('git_repo', sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True))
    op.add_column('git_repo', sa.Column('description', sa.Text(), server_default='', nullable=False))
    op.add_column('git_repo', sa.Column('owner_user_id', sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE git_repo r SET slug = b.slug, name = b.display_name, "
        "description = COALESCE(b.description, ''), owner_user_id = b.publisher_user_id "
        "FROM agent_bundle b WHERE b.git_repo_id = r.id"
    )
    # A repo with no bundle (a mirror) still needs the NOT NULL columns.
    op.execute(
        "UPDATE git_repo SET slug = 'repo-' || left(id::text, 8), name = 'repo-' || left(id::text, 8) "
        "WHERE slug IS NULL"
    )
    op.alter_column('git_repo', 'slug', nullable=False)
    op.alter_column('git_repo', 'name', nullable=False)
    op.create_foreign_key(
        'git_repo_owner_user_id_fkey', 'git_repo', 'user',
        ['owner_user_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index('ix_git_repo_owner_user_id', 'git_repo', ['owner_user_id'], unique=False)
    op.create_index(
        'uq_git_repo_agent_slug', 'git_repo', ['slug'],
        unique=True, postgresql_where=sa.text("kind = 'agent'"),
    )

    # 2. git_repo_grant, shape of 0e62eb9df5d4.
    op.create_table(
        'git_repo_grant',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('repo_id', sa.Uuid(), nullable=False),
        sa.Column('principal_kind', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('principal_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('access', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('granted_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['granted_by_user_id'], ['user.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['repo_id'], ['git_repo.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repo_id', 'principal_kind', 'principal_id', name='uq_git_repo_grant_principal'),
    )
    op.create_index('ix_git_repo_grant_repo_id', 'git_repo_grant', ['repo_id'], unique=False)
    op.create_index('ix_git_repo_grant_user_id', 'git_repo_grant', ['user_id'], unique=False)
    op.execute(
        "INSERT INTO git_repo_grant (id, repo_id, principal_kind, principal_id, user_id, access, "
        "granted_by_user_id, created_at) "
        "SELECT gen_random_uuid(), b.git_repo_id, g.principal_kind, g.principal_id, g.user_id, "
        "CASE g.access WHEN 'use' THEN 'read' WHEN 'edit' THEN 'write' ELSE 'maintain' END, "
        "g.granted_by_user_id, g.created_at "
        "FROM bundle_access_grant g JOIN agent_bundle b ON b.id = g.bundle_id "
        "WHERE b.runs_on = 'desktop'"
    )
    op.execute(
        "INSERT INTO git_repo_grant (id, repo_id, principal_kind, principal_id, user_id, access) "
        "SELECT gen_random_uuid(), git_repo_id, 'everyone', '*', NULL, 'read' "
        "FROM agent_bundle WHERE runs_on = 'desktop' AND visibility = 'public'"
    )

    # 3. Desktop bundles go; revisions and grants cascade.
    op.execute("DELETE FROM agent_bundle WHERE runs_on = 'desktop'")

    # 4. Columns back to the C1 shape.
    op.alter_column(
        'agent_bundle_revision', 'snapshot_path',
        existing_type=sa.String(length=1024), nullable=False,
    )
    op.drop_column('agent_bundle_revision', 'git_commit_sha')
    op.drop_constraint('ck_agent_bundle_desktop_fields', 'agent_bundle', type_='check')
    op.drop_index(
        'uq_agent_bundle_desktop_slug', table_name='agent_bundle',
        postgresql_where=sa.text("runs_on = 'desktop'"),
    )
    op.drop_index(
        'uq_agent_bundle_desktop_manifest_id', table_name='agent_bundle',
        postgresql_where=sa.text("runs_on = 'desktop'"),
    )
    op.drop_constraint('agent_bundle_git_repo_id_key', 'agent_bundle', type_='unique')
    op.drop_constraint('agent_bundle_git_repo_id_fkey', 'agent_bundle', type_='foreignkey')
    op.drop_column('agent_bundle', 'slug')
    op.drop_column('agent_bundle', 'manifest_id')
    op.drop_column('agent_bundle', 'git_repo_id')
    op.drop_column('agent_bundle', 'runs_on')
