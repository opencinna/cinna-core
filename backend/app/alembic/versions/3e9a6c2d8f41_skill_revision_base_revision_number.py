"""skill_revision_base_revision_number

Skill co-maintainers: ``skill_package_revision.base_revision_number``, the
revision the sharer's copy held when they shared this one. Nullable, no
backfill: older revisions have no recorded base.

Revision ID: 3e9a6c2d8f41
Revises: 7c1d4e8a2b90
Create Date: 2026-11-05 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '3e9a6c2d8f41'
down_revision = '7c1d4e8a2b90'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'skill_package_revision',
        sa.Column('base_revision_number', sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column('skill_package_revision', 'base_revision_number')
