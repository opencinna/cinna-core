"""credential allow_bundle_reuse

Owner consent for recipients to embed a shared agent_api connection as
publisher-provided in their own bundles and skills. Also makes
``credential_shares.shared_by_user_id`` cascade on user delete: a re-sharer's
shares go with their account.

Revision ID: 713c48f71fc6
Revises: 46e56c924dd3
Create Date: 2026-11-12 12:45:24.539612

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '713c48f71fc6'
down_revision = '46e56c924dd3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'credential',
        sa.Column(
            'allow_bundle_reuse',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.drop_constraint(
        'credential_shares_shared_by_user_id_fkey',
        'credential_shares',
        type_='foreignkey',
    )
    op.create_foreign_key(
        'credential_shares_shared_by_user_id_fkey',
        'credential_shares',
        'user',
        ['shared_by_user_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade():
    op.drop_constraint(
        'credential_shares_shared_by_user_id_fkey',
        'credential_shares',
        type_='foreignkey',
    )
    op.create_foreign_key(
        'credential_shares_shared_by_user_id_fkey',
        'credential_shares',
        'user',
        ['shared_by_user_id'],
        ['id'],
    )
    op.drop_column('credential', 'allow_bundle_reuse')
