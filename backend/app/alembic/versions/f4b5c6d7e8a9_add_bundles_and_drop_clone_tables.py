"""add bundles, bundle revisions, grants; drop share/clone tables

Revision ID: f4b5c6d7e8a9
Revises: e3f4a5b6c7d8
Create Date: 2026-05-06 14:00:00.000000

Phase 2 of the Agent Bundles & Installs plan.

Schema changes:

- New tables:
    * ``agent_bundle`` — canonical bundle metadata.
    * ``agent_bundle_revision`` — append-only snapshot per publish.
    * ``bundle_access_grant`` — explicit per-user catalog access.

- ``agent`` column changes:
    * Add ``bundle_uuid`` (FK → agent_bundle, ON DELETE SET NULL).
    * Add ``installed_revision_id`` (FK → agent_bundle_revision, ON DELETE SET NULL).
    * Add ``is_publisher_install`` (bool, default false).
    * Drop ``is_clone``, ``parent_agent_id``, ``clone_mode``.
    * Drop ``ix_agent_is_clone``, ``ix_agent_parent`` partial indexes.
    * Drop ``fk_agent_parent`` FK.
    * Add partial unique index ``uq_agent_publisher_install_per_bundle``
      on ``(bundle_uuid)`` where ``is_publisher_install = true``.
    * Add partial index ``ix_agent_bundle_uuid`` on ``(bundle_uuid)``
      where ``bundle_uuid IS NOT NULL``.
    * Add unique constraint ``uq_agent_bundle_id_per_publisher`` on
      ``(owner_id, bundle_id)``.

- Drop ``agent_share`` and ``clone_update_request`` tables (with their FKs
  cascading away).

Data migration policy: per the brief, "no backward compatibility — clones
simply become standalone installs without a bundle reference." We DO NOT
backfill ``bundle_uuid`` from any prior parent/clone link; all existing
agents simply remain unpublished standalone installs after this migration
runs. The publisher's "Publish" UI promotes each agent to a bundle on demand.

Downgrade IS NOT SUPPORTED — recreating share/clone tables empty after the
fact would silently break any installs created post-migration. Document
this loudly in the docstring; the body raises so the operator sees it.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f4b5c6d7e8a9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. New tables ────────────────────────────────────────────

    op.create_table(
        "agent_bundle",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("publisher_user_id", sa.Uuid(), nullable=False),
        sa.Column("latest_revision_id", sa.Uuid(), nullable=True),
        sa.Column("is_listed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="private"),
        sa.Column(
            "default_install_mode",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["publisher_user_id"], ["user.id"], ondelete="RESTRICT"),
        # latest_revision_id FK is added after agent_bundle_revision exists
        # to break the circular dependency.
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bundle_id", name="uq_agent_bundle_bundle_id"),
    )
    op.create_index(
        "ix_agent_bundle_publisher", "agent_bundle", ["publisher_user_id"]
    )
    op.create_index(
        "ix_agent_bundle_listed_visibility",
        "agent_bundle",
        ["is_listed", "visibility"],
        postgresql_where=sa.text("is_listed = true"),
    )

    op.create_table(
        "agent_bundle_revision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("workflow_prompt", sa.Text(), nullable=True),
        sa.Column("entrypoint_prompt", sa.Text(), nullable=True),
        sa.Column("refiner_prompt", sa.Text(), nullable=True),
        sa.Column("agent_sdk_building", sa.String(length=128), nullable=True),
        sa.Column("agent_sdk_conversation", sa.String(length=128), nullable=True),
        sa.Column("model_override_building", sa.String(length=128), nullable=True),
        sa.Column("model_override_conversation", sa.String(length=128), nullable=True),
        sa.Column(
            "required_credential_specs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("snapshot_path", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["bundle_id"], ["agent_bundle.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bundle_id", "revision_number", name="uq_revision_bundle_number"
        ),
    )
    op.create_index(
        "ix_revision_bundle", "agent_bundle_revision", ["bundle_id"]
    )

    # Now add agent_bundle.latest_revision_id FK (deferred).
    op.create_foreign_key(
        "fk_agent_bundle_latest_revision",
        "agent_bundle",
        "agent_bundle_revision",
        ["latest_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "bundle_access_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["bundle_id"], ["agent_bundle.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bundle_id", "user_id", name="uq_bundle_grant_bundle_user"
        ),
    )
    op.create_index(
        "ix_bundle_grant_bundle", "bundle_access_grant", ["bundle_id"]
    )
    op.create_index(
        "ix_bundle_grant_user", "bundle_access_grant", ["user_id"]
    )

    # ── 2. Agent column changes ─────────────────────────────────

    # Drop the named FK + indexes that target the going-away columns first.
    # The FK and indexes were defined in the historical migration; their
    # exact names match the model.
    with op.batch_alter_table("agent") as batch:
        try:
            batch.drop_constraint("fk_agent_parent", type_="foreignkey")
        except Exception:
            pass

    # Drop partial indexes on is_clone / parent_agent_id.
    op.execute("DROP INDEX IF EXISTS ix_agent_is_clone")
    op.execute("DROP INDEX IF EXISTS ix_agent_parent")

    # Add new columns.
    op.add_column(
        "agent",
        sa.Column("bundle_uuid", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "agent",
        sa.Column("installed_revision_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "agent",
        sa.Column(
            "is_publisher_install",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_foreign_key(
        "fk_agent_bundle_uuid",
        "agent",
        "agent_bundle",
        ["bundle_uuid"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_agent_installed_revision",
        "agent",
        "agent_bundle_revision",
        ["installed_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Drop clone columns.
    op.drop_column("agent", "is_clone")
    op.drop_column("agent", "parent_agent_id")
    op.drop_column("agent", "clone_mode")

    # New indexes / constraints.
    op.create_index(
        "ix_agent_bundle_uuid",
        "agent",
        ["bundle_uuid"],
        postgresql_where=sa.text("bundle_uuid IS NOT NULL"),
    )
    op.create_index(
        "uq_agent_publisher_install_per_bundle",
        "agent",
        ["bundle_uuid"],
        unique=True,
        postgresql_where=sa.text("is_publisher_install = true"),
    )
    op.create_unique_constraint(
        "uq_agent_bundle_id_per_publisher",
        "agent",
        ["owner_id", "bundle_id"],
    )

    # ── 3. Drop share / clone tables ────────────────────────────

    op.drop_table("clone_update_request")
    op.drop_table("agent_share")


def downgrade():
    """Downgrade is NOT supported.

    Recreating share / clone tables empty would silently break any installs
    created post-migration. If you need to roll back, restore from a
    pre-Phase-2 backup.
    """
    raise NotImplementedError(
        "Downgrade of f4b5c6d7e8a9 is not supported — restore from backup."
    )
