"""credential share revocation

The credential owner's removal of one person's share, so no install path
(provision-local, cloud bundle or skill install) shares it with them again
until the owner shares it directly.

Revision ID: bfd8b2ff2e3c
Revises: 3c1f7a2d9e40
Create Date: 2026-10-11 08:27:44.490023

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'bfd8b2ff2e3c'
down_revision = '3c1f7a2d9e40'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'credential_share_revocation',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('credential_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('revoked_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['credential_id'], ['credential.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['revoked_by_user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'credential_id',
            'user_id',
            name='uq_credential_share_revocation_credential_user',
        ),
    )


def downgrade():
    op.drop_table('credential_share_revocation')
