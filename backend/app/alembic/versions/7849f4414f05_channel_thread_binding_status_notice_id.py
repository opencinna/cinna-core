"""channel thread binding status notice id

Revision ID: 7849f4414f05
Revises: 867cacb5a827
Create Date: 2026-08-26 11:02:58.478954

The id of the thread's live status notice — the single progress message the
pipeline rewrites in place and deletes when the agent's reply lands. Nullable
with no backfill: NULL is the resting state, so every existing binding is
already correct.

The autogenerate run also proposed three ``cli_device_login_request`` timestamp
alterations. Those are dropped deliberately: the drift is in the *model*, which
declares naive datetimes against a column that is correctly ``timestamptz``, and
"fixing" the database to match would be the wrong direction.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '7849f4414f05'
down_revision = '867cacb5a827'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'channel_thread_binding',
        sa.Column(
            'status_message_id',
            sqlmodel.sql.sqltypes.AutoString(length=255),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('channel_thread_binding', 'status_message_id')
