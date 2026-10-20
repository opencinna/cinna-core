"""artifact link token_encrypted

Stores the link token Fernet-encrypted so the owner can copy a link URL again.
Nullable, no backfill: links created before this revision only have the hash,
their plaintext is gone and their URL stays null.

Revision ID: 6d9138d0019c
Revises: be8f767e32ed
Create Date: 2026-10-20 08:59:14.355761

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6d9138d0019c'
down_revision = 'be8f767e32ed'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('artifact_link', sa.Column('token_encrypted', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('artifact_link', 'token_encrypted')
