"""channel_turn_delivery ledger

Revision ID: 1367eb81ac66
Revises: 7849f4414f05
Create Date: 2026-09-01 18:20:31.674819

The per-turn outbound delivery ledger for server channels — one row per
external message a channel turn wrote (draft / sealed / final). See
``app/models/server_channels/channel_turn_delivery.py``.

Autogenerate also proposed three ``cli_device_login_request`` timestamp
``alter_column``s (timezone-aware in the database, naive in the model). That is
pre-existing drift on an unrelated table, the model is the side that is wrong,
and applying it would silently strip the timezone from live rows — stripped
out of this revision deliberately.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '1367eb81ac66'
down_revision = '7849f4414f05'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'channel_turn_delivery',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('binding_id', sa.Uuid(), nullable=False),
        # Nullable: a boundary write records what it honestly knows, and turn
        # identity is only handed over by the terminal stream event at the end
        # of the batch. See the model's module docstring.
        sa.Column('session_message_id', sa.Uuid(), nullable=True),
        sa.Column('part_index', sa.Integer(), nullable=False),
        sa.Column('role', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('external_message_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('visible_char_end', sa.Integer(), nullable=True),
        sa.Column('content_sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['binding_id'], ['channel_thread_binding.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_message_id'], ['message.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_message_id', 'part_index', name='uq_channel_turn_delivery_part'),
    )
    op.create_index(
        op.f('ix_channel_turn_delivery_binding_id'),
        'channel_turn_delivery',
        ['binding_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_channel_turn_delivery_session_message_id'),
        'channel_turn_delivery',
        ['session_message_id'],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_channel_turn_delivery_session_message_id'),
        table_name='channel_turn_delivery',
    )
    op.drop_index(
        op.f('ix_channel_turn_delivery_binding_id'),
        table_name='channel_turn_delivery',
    )
    op.drop_table('channel_turn_delivery')
