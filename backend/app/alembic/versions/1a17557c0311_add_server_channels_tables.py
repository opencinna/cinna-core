"""add_server_channels_tables

Creates the three Server Channels tables:

* ``server_channel``            — one row per admin-configured channel instance
* ``server_auto_install_bundle``— the server-wide auto-install catalog list
* ``channel_thread_binding``    — (channel, thread) → (user, agent, session)

Hand-corrected after autogenerate: the generated file also proposed dropping
``session.channel_*`` columns/indexes, ``app_agent_route.channels``,
``user_app_agent_route.channels`` and altering ``cli_device_login_request``
timestamp types. Those are pre-existing drift in local developer databases
(no model and no committed migration references them), unrelated to this
feature, and are deliberately NOT touched here.

Revision ID: 1a17557c0311
Revises: 227785421f7a
Create Date: 2026-08-21 13:50:50.355659

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '1a17557c0311'
down_revision = '227785421f7a'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'server_channel',
        sa.Column('channel_type', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('auto_register_users', sa.Boolean(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('encrypted_secrets', sa.Text(), nullable=True),
        sa.Column('email_whitelist', sa.Text(), nullable=True),
        sa.Column('webhook_token', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        # Deleting the admin who created a channel must not delete the channel.
        sa.ForeignKeyConstraint(['created_by'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_server_channel_name'),
        # Also serves as the lookup index for the per-request token resolve.
        sa.UniqueConstraint('webhook_token', name='uq_server_channel_webhook_token'),
    )
    op.create_index(
        op.f('ix_server_channel_channel_type'), 'server_channel', ['channel_type'], unique=False
    )

    op.create_table(
        'server_auto_install_bundle',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('bundle_uuid', sa.Uuid(), nullable=False),
        sa.Column('added_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['added_by'], ['user.id'], ondelete='SET NULL'),
        # Deleting the bundle removes the list entry; existing installs stay.
        sa.ForeignKeyConstraint(['bundle_uuid'], ['agent_bundle.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bundle_uuid', name='uq_server_auto_install_bundle_bundle_uuid'),
    )

    op.create_table(
        'channel_thread_binding',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('server_channel_id', sa.Uuid(), nullable=False),
        sa.Column('thread_key', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('agent_id', sa.Uuid(), nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column('pending_messages', sa.JSON(), nullable=False),
        sa.Column('last_external_message_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        # Uninstalling the agent drops the binding ⇒ next message re-routes.
        sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['server_channel_id'], ['server_channel.id'], ondelete='CASCADE'),
        # Deleting the session only unbinds it ⇒ next message opens a fresh one.
        sa.ForeignKeyConstraint(['session_id'], ['session.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # The race guard for two first-messages arriving in one new thread.
        sa.UniqueConstraint(
            'server_channel_id', 'thread_key', name='uq_channel_thread_binding_thread'
        ),
    )
    op.create_index(
        op.f('ix_channel_thread_binding_session_id'),
        'channel_thread_binding',
        ['session_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_channel_thread_binding_user_id'),
        'channel_thread_binding',
        ['user_id'],
        unique=False,
    )


def downgrade():
    # Bindings first (they FK the channel), then the list, then the channel.
    op.drop_index(op.f('ix_channel_thread_binding_user_id'), table_name='channel_thread_binding')
    op.drop_index(op.f('ix_channel_thread_binding_session_id'), table_name='channel_thread_binding')
    op.drop_table('channel_thread_binding')
    op.drop_table('server_auto_install_bundle')
    op.drop_index(op.f('ix_server_channel_channel_type'), table_name='server_channel')
    op.drop_table('server_channel')
