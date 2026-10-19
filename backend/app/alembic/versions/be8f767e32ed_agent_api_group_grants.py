"""agent api group grants: producer API scopes granted to a user group

docs/plans/user_groups_scopes_addendum.md §1. A caller's effective scopes are
their personal grant's scopes plus those of every group they actively belong
to, resolved live.

``created_by`` is SET NULL DEFERRABLE INITIALLY DEFERRED (user groups plan
D11, written explicitly since autogenerate drops it): deleting the producer
owner reaches the row through the agent CASCADE and through ``created_by``.

Revision ID: be8f767e32ed
Revises: 7896e6f51c03
Create Date: 2026-10-19 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'be8f767e32ed'
down_revision = '7896e6f51c03'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'agent_api_access_group_grant',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('producer_agent_id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=False),
        sa.Column('scopes', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['producer_agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_group.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['created_by'], ['user.id'],
            ondelete='SET NULL', deferrable=True, initially='DEFERRED',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'producer_agent_id', 'group_id',
            name='uq_agent_api_access_group_grant_producer_group',
        ),
    )
    op.create_index(
        op.f('ix_agent_api_access_group_grant_producer_agent_id'),
        'agent_api_access_group_grant', ['producer_agent_id'], unique=False,
    )
    op.create_index(
        op.f('ix_agent_api_access_group_grant_group_id'),
        'agent_api_access_group_grant', ['group_id'], unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_agent_api_access_group_grant_group_id'),
        table_name='agent_api_access_group_grant',
    )
    op.drop_index(
        op.f('ix_agent_api_access_group_grant_producer_agent_id'),
        table_name='agent_api_access_group_grant',
    )
    op.drop_table('agent_api_access_group_grant')
