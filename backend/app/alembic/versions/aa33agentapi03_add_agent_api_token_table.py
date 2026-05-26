"""add agent_api_token table

Revision ID: aa33agentapi03
Revises: aa22agentapi02
Create Date: 2026-05-25 11:00:00.000000

"""
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op


# revision identifiers, used by Alembic.
revision = 'aa33agentapi03'
down_revision = 'aa22agentapi02'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'agent_api_token',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('agent_id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('credential_id', sa.Uuid(), nullable=True),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('token_prefix', sqlmodel.sql.sqltypes.AutoString(length=12), nullable=False),
        sa.Column('label', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('read_only_override', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['credential_id'], ['credential.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_agent_api_token_token_hash', 'agent_api_token', ['token_hash'], unique=True
    )
    op.create_index(
        'ix_agent_api_token_agent_id', 'agent_api_token', ['agent_id'], unique=False
    )
    op.create_index(
        'ix_agent_api_token_credential_id', 'agent_api_token', ['credential_id'], unique=False
    )


def downgrade():
    op.drop_index('ix_agent_api_token_credential_id', table_name='agent_api_token')
    op.drop_index('ix_agent_api_token_agent_id', table_name='agent_api_token')
    op.drop_index('ix_agent_api_token_token_hash', table_name='agent_api_token')
    op.drop_table('agent_api_token')
