"""plugin snapshots

Content snapshots and component analysis for every marketplace format.

Backfill: ``skills`` rows copy their ``skill_archive_*`` pair (``ready`` when a
digest exists, else ``failed`` with the row's ``unsupported_reason``) and get a
one-skill ``components`` block from ``config.skill``. ``claude`` / ``codex``
rows become ``pending``: the snapshot worker's first ticks snapshot them, which
is the "snapshot all" bootstrap.

Revision ID: a2c161d383aa
Revises: d26d5d827d17
Create Date: 2026-10-24 10:33:14.772917

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'a2c161d383aa'
down_revision = 'd26d5d827d17'
branch_labels = None
depends_on = None

TABLE = 'llm_plugin_marketplace_plugin'


def upgrade():
    op.add_column(TABLE, sa.Column('archive_sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column(TABLE, sa.Column('archive_size', sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_reason', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_source_key', sqlmodel.sql.sqltypes.AutoString(length=1024), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_commit', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_ref', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_at', sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_attempts', sa.Integer(), server_default='0', nullable=False))
    op.add_column(TABLE, sa.Column('snapshot_claim_id', sa.Uuid(), nullable=True))
    op.add_column(TABLE, sa.Column('snapshot_lease_until', sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column('components', sa.JSON(), nullable=True))
    op.add_column(TABLE, sa.Column('runs_code', sa.Boolean(), nullable=True))
    op.create_index(
        'ix_plugin_snapshot_pending',
        TABLE,
        ['snapshot_lease_until'],
        unique=False,
        postgresql_where=sa.text("snapshot_status = 'pending'"),
    )

    # skills rows: mirror the existing archive pair and the one skill.
    op.execute(
        f"""
        UPDATE {TABLE} SET
            archive_sha256 = skill_archive_sha256,
            archive_size = skill_archive_size,
            snapshot_status = CASE WHEN skill_archive_sha256 IS NOT NULL
                                   THEN 'ready' ELSE 'failed' END,
            snapshot_reason = CASE WHEN skill_archive_sha256 IS NOT NULL
                                   THEN NULL ELSE unsupported_reason END,
            snapshot_source_key = 'local:' || source_path,
            runs_code = COALESCE((config -> 'skill' ->> 'has_scripts')::boolean, false),
            components = json_build_object(
                'skills', CASE WHEN json_typeof(config -> 'skill') = 'object'
                                    AND (config -> 'skill' ->> 'name') IS NOT NULL
                               THEN json_build_array(json_build_object(
                                   'name', config -> 'skill' ->> 'name',
                                   'has_scripts', COALESCE((config -> 'skill' ->> 'has_scripts')::boolean, false)))
                               ELSE '[]'::json END,
                'commands', '[]'::json,
                'agents', '[]'::json,
                'hooks', NULL,
                'mcp_servers', '[]'::json,
                'lsp_servers', '[]'::json)
        WHERE plugin_type = 'skills'
        """
    )
    # claude / codex rows: queued for the worker's bootstrap pass.
    op.execute(
        f"UPDATE {TABLE} SET snapshot_status = 'pending' "
        f"WHERE plugin_type IN ('claude', 'codex')"
    )


def downgrade():
    op.drop_index(
        'ix_plugin_snapshot_pending',
        table_name=TABLE,
        postgresql_where=sa.text("snapshot_status = 'pending'"),
    )
    for column in (
        'runs_code', 'components', 'snapshot_lease_until', 'snapshot_claim_id',
        'snapshot_attempts', 'snapshot_at', 'snapshot_ref', 'snapshot_commit',
        'snapshot_source_key', 'snapshot_reason', 'snapshot_status',
        'archive_size', 'archive_sha256',
    ):
        op.drop_column(TABLE, column)
