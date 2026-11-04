"""add_dashboard_components

Agent dashboard components (Dashboards C2): the cached result of an
``agent_component`` block, the per-target runtime row (run budget and page
highlight readiness), and ``session.dashboard_id`` for the dashboard chat,
backfilled from the block that opened each prompt-action session.

The ``agent_component`` view type needs no DDL: ``user_dashboard_block.view_type``
is a plain VARCHAR(50) validated by the model.

Revision ID: 056d6b9d9f87
Revises: 5b7e2f9c1a3d
Create Date: 2026-11-04 09:30:45.434669

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '056d6b9d9f87'
down_revision = '5b7e2f9c1a3d'
branch_labels = None
depends_on = None

SESSION_DASHBOARD_FK = 'session_dashboard_id_fkey'


def upgrade():
    op.create_table(
        'dashboard_tile_runtime',
        sa.Column('target_key', sqlmodel.sql.sqltypes.AutoString(length=400), nullable=False),
        sa.Column('run_times', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('page_doc', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('highlight_ready', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('page_seq', sa.BigInteger(), nullable=True),
        sa.Column('page_state_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('target_key'),
    )
    op.create_index(
        'ix_dashboard_tile_runtime_updated_at', 'dashboard_tile_runtime', ['updated_at'], unique=False
    )
    op.create_table(
        'user_dashboard_block_result',
        sa.Column('block_id', sa.Uuid(), nullable=False),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('params_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('mark', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column('mark_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consumed_schedule_log_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['block_id'], ['user_dashboard_block.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('block_id'),
    )
    op.add_column('session', sa.Column('dashboard_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_session_dashboard_id'), 'session', ['dashboard_id'], unique=False)
    op.create_foreign_key(
        SESSION_DASHBOARD_FK, 'session', 'user_dashboard', ['dashboard_id'], ['id'], ondelete='SET NULL'
    )
    # Prompt-action sessions opened from a block belong to that block's dashboard.
    op.execute(
        """
        UPDATE session AS s
        SET dashboard_id = b.dashboard_id
        FROM user_dashboard_block AS b
        JOIN user_dashboard AS d ON d.id = b.dashboard_id
        WHERE s.dashboard_block_id = b.id
          AND d.owner_id = s.user_id
          AND s.dashboard_id IS NULL
        """
    )


def downgrade():
    op.drop_constraint(SESSION_DASHBOARD_FK, 'session', type_='foreignkey')
    op.drop_index(op.f('ix_session_dashboard_id'), table_name='session')
    op.drop_column('session', 'dashboard_id')
    op.drop_table('user_dashboard_block_result')
    op.drop_index('ix_dashboard_tile_runtime_updated_at', table_name='dashboard_tile_runtime')
    op.drop_table('dashboard_tile_runtime')
