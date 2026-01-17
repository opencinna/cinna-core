"""add share ai credentials

Revision ID: j0e1f2g3h4i5
Revises: i9d0e1f2g3h4
Create Date: 2026-01-16

Adds AI credential provision fields to agent_share table:
- provide_ai_credentials: Whether owner provides their AI credentials
- conversation_ai_credential_id: FK to ai_credential for conversation SDK
- building_ai_credential_id: FK to ai_credential for building SDK
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'j0e1f2g3h4i5'
down_revision = 'i9d0e1f2g3h4'
branch_labels = None
depends_on = None


def upgrade():
    # Add provide_ai_credentials flag
    op.add_column(
        'agent_share',
        sa.Column('provide_ai_credentials', sa.Boolean(), nullable=False, server_default='false')
    )

    # Add conversation_ai_credential_id FK
    op.add_column(
        'agent_share',
        sa.Column('conversation_ai_credential_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_agent_share_conversation_ai_credential',
        'agent_share',
        'ai_credential',
        ['conversation_ai_credential_id'],
        ['id'],
        ondelete='SET NULL'
    )

    # Add building_ai_credential_id FK
    op.add_column(
        'agent_share',
        sa.Column('building_ai_credential_id', sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        'fk_agent_share_building_ai_credential',
        'agent_share',
        'ai_credential',
        ['building_ai_credential_id'],
        ['id'],
        ondelete='SET NULL'
    )


def downgrade():
    # Drop foreign keys first
    op.drop_constraint('fk_agent_share_building_ai_credential', 'agent_share', type_='foreignkey')
    op.drop_constraint('fk_agent_share_conversation_ai_credential', 'agent_share', type_='foreignkey')

    # Drop columns
    op.drop_column('agent_share', 'building_ai_credential_id')
    op.drop_column('agent_share', 'conversation_ai_credential_id')
    op.drop_column('agent_share', 'provide_ai_credentials')
