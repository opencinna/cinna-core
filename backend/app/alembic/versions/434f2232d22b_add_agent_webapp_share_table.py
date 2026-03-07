"""add agent_webapp_share table

Revision ID: 434f2232d22b
Revises: 5332c5643236
Create Date: 2026-03-07 10:00:35.223855

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '434f2232d22b'
down_revision = '5332c5643236'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('agent_webapp_share',
    sa.Column('label', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('token_prefix', sqlmodel.sql.sqltypes.AutoString(length=12), nullable=False),
    sa.Column('token', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('allow_data_api', sa.Boolean(), nullable=False),
    sa.Column('security_code_encrypted', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('failed_code_attempts', sa.Integer(), nullable=False),
    sa.Column('is_code_blocked', sa.Boolean(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_webapp_share_agent_id'), 'agent_webapp_share', ['agent_id'], unique=False)
    op.create_index(op.f('ix_agent_webapp_share_owner_id'), 'agent_webapp_share', ['owner_id'], unique=False)
    op.create_index(op.f('ix_agent_webapp_share_token_hash'), 'agent_webapp_share', ['token_hash'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_agent_webapp_share_token_hash'), table_name='agent_webapp_share')
    op.drop_index(op.f('ix_agent_webapp_share_owner_id'), table_name='agent_webapp_share')
    op.drop_index(op.f('ix_agent_webapp_share_agent_id'), table_name='agent_webapp_share')
    op.drop_table('agent_webapp_share')
