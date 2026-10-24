"""marketplace access mode, per-entry access, addons policy flags

Every existing marketplace becomes ``allow_all`` (nothing changes for it);
``llm_plugin_entry_access`` keeps allow / ban verdicts by entry name; the four
addons policy flags default to true (today's behaviour).

Revision ID: 48c3eee7b6cd
Revises: ab48f9755987
Create Date: 2026-10-24 12:01:35.722711

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '48c3eee7b6cd'
down_revision = 'ab48f9755987'
branch_labels = None
depends_on = None

_POLICY_COLUMNS = (
    'addons_external_directories',
    'addons_custom_mcp',
    'addons_plugin_hooks',
    'addons_stdio_mcp',
)


def upgrade():
    op.create_table('llm_plugin_entry_access',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('marketplace_id', sa.Uuid(), nullable=False),
    sa.Column('entry_name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('access', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('changed_by_id', sa.Uuid(), nullable=True),
    sa.Column('changed_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['changed_by_id'], ['user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['marketplace_id'], ['llm_plugin_marketplace.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_llm_plugin_entry_access_entry', 'llm_plugin_entry_access', ['marketplace_id', 'entry_name'], unique=True)
    op.add_column('llm_plugin_marketplace', sa.Column('access_mode', sqlmodel.sql.sqltypes.AutoString(length=16), server_default='allow_all', nullable=False))
    for column in _POLICY_COLUMNS:
        op.add_column('server_config', sa.Column(column, sa.Boolean(), server_default='true', nullable=False))


def downgrade():
    for column in reversed(_POLICY_COLUMNS):
        op.drop_column('server_config', column)
    op.drop_column('llm_plugin_marketplace', 'access_mode')
    op.drop_index('uq_llm_plugin_entry_access_entry', table_name='llm_plugin_entry_access')
    op.drop_table('llm_plugin_entry_access')
