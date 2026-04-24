"""add_admin_env_fields

Revision ID: d40c20201e5b
Revises: d0e1f2g3h4i5
Create Date: 2026-04-24 14:00:29.424950

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd40c20201e5b'
down_revision = 'd0e1f2g3h4i5'
branch_labels = None
depends_on = None


def upgrade():
    # Add last_build_at (set on successful rebuild, used by admin console to detect staleness)
    op.add_column('agent_environment', sa.Column('last_build_at', sa.DateTime(timezone=True), nullable=True))
    # Add current_image_tag (set whenever _update_environment_config runs with an image tag)
    op.add_column('agent_environment', sa.Column('current_image_tag', sa.String(length=255), nullable=True))
    op.create_index(op.f('ix_agent_environment_current_image_tag'), 'agent_environment', ['current_image_tag'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_agent_environment_current_image_tag'), table_name='agent_environment')
    op.drop_column('agent_environment', 'current_image_tag')
    op.drop_column('agent_environment', 'last_build_at')
