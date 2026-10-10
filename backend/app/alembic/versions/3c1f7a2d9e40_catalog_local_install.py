"""catalog_local_install: local installs reported by Cinna Desktop

One row per (person, device, item, host agent): agents a person got on a
computer and catalog skills pinned into a local agent (README U10). The unique
constraint treats a NULL ``host_manifest_id`` (agents) as equal
(``NULLS NOT DISTINCT``, PG15+). ``item_uuid`` has no FK (two target tables).

Downgrade drops the table.

Revision ID: 3c1f7a2d9e40
Revises: 6b4297a50868
Create Date: 2026-10-10 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = "3c1f7a2d9e40"
down_revision = "6b4297a50868"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_local_install",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("item_kind", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("item_uuid", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("host_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "device_id",
            "item_kind",
            "item_uuid",
            "host_manifest_id",
            name="uq_catalog_local_install",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_catalog_local_install_item",
        "catalog_local_install",
        ["item_kind", "item_uuid"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_catalog_local_install_item", table_name="catalog_local_install")
    op.drop_table("catalog_local_install")
