"""add credential service_uri

Revision ID: 3f8f2a2e7f23
Revises: d54391bd8cf2
Create Date: 2026-06-02 17:15:45.538197

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3f8f2a2e7f23'
down_revision = 'd54391bd8cf2'
branch_labels = None
depends_on = None


def upgrade():
    # Non-secret audience/slot id (I4). Nullable plaintext, no encryption.
    # Existing rows backfill to NULL = legacy behavior (I5).
    op.add_column('credential', sa.Column('service_uri', sa.Text(), nullable=True))
    # Partial index — the matcher filters on service_uri = :value AND type;
    # the vast majority of rows are NULL, so a partial index stays small.
    op.create_index(
        'ix_credential_service_uri',
        'credential',
        ['service_uri'],
        unique=False,
        postgresql_where=sa.text('service_uri IS NOT NULL'),
    )


def downgrade():
    op.drop_index(
        'ix_credential_service_uri',
        table_name='credential',
        postgresql_where=sa.text('service_uri IS NOT NULL'),
    )
    op.drop_column('credential', 'service_uri')
