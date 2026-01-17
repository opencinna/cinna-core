"""add env ai credentials

Revision ID: k1f2g3h4i5j6
Revises: j0e1f2g3h4i5
Create Date: 2026-01-16

Adds AI credential linking fields to agent_environment table:
- use_default_ai_credentials: Whether to use user's default credentials
- conversation_ai_credential_id: FK to ai_credential for conversation SDK
- building_ai_credential_id: FK to ai_credential for building SDK
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'k1f2g3h4i5j6'
down_revision = 'j0e1f2g3h4i5'
branch_labels = None
depends_on = None


def upgrade():
    # Add use_default_ai_credentials flag (default True for backward compat)
    op.add_column(
        'agent_environment',
        sa.Column('use_default_ai_credentials', sa.Boolean(), nullable=False, server_default='true')
    )

    # Add conversation_ai_credential_id FK
    op.add_column(
        'agent_environment',
        sa.Column('conversation_ai_credential_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_agent_environment_conversation_ai_credential',
        'agent_environment',
        'ai_credential',
        ['conversation_ai_credential_id'],
        ['id'],
        ondelete='SET NULL'
    )

    # Add building_ai_credential_id FK
    op.add_column(
        'agent_environment',
        sa.Column('building_ai_credential_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_agent_environment_building_ai_credential',
        'agent_environment',
        'ai_credential',
        ['building_ai_credential_id'],
        ['id'],
        ondelete='SET NULL'
    )


def downgrade():
    # Drop foreign keys first
    op.drop_constraint('fk_agent_environment_building_ai_credential', 'agent_environment', type_='foreignkey')
    op.drop_constraint('fk_agent_environment_conversation_ai_credential', 'agent_environment', type_='foreignkey')

    # Drop columns
    op.drop_column('agent_environment', 'building_ai_credential_id')
    op.drop_column('agent_environment', 'conversation_ai_credential_id')
    op.drop_column('agent_environment', 'use_default_ai_credentials')
