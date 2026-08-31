"""email becomes a server channel

Revision ID: ca0192122e0c
Revises: 458913091bd5
Create Date: 2026-08-25 01:04:06.309424

Phase 4 of the channels & identity unification: the per-agent email
integration is gone, and email joins the platform as a server channel like
any other transport.

Four changes, and nothing else:

* drop ``agent_email_integration`` — the per-agent mailbox table.
* drop ``mail_server_config.user_id`` — mail server configs become
  server-scoped rather than user-owned.
* drop ``input_task.source_email_message_id`` and ``input_task.source_agent_id``
  — the email-specific task provenance columns.
* ``server_channel.webhook_token`` becomes nullable — a polled transport is
  not reached by a webhook and must not carry a token. NULL, never ``''``:
  ``uq_server_channel_webhook_token`` is a plain UNIQUE constraint, and in
  PostgreSQL ``''`` is a value that a second tokenless channel would collide
  on, while UNIQUE permits any number of NULLs. That constraint is untouched
  here — only the column's nullability changes.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'ca0192122e0c'
down_revision = '458913091bd5'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('agent_email_integration')
    op.drop_constraint(op.f('fk_input_task_source_agent_id'), 'input_task', type_='foreignkey')
    op.drop_constraint(op.f('fk_input_task_source_email_message_id'), 'input_task', type_='foreignkey')
    op.drop_column('input_task', 'source_agent_id')
    op.drop_column('input_task', 'source_email_message_id')
    op.drop_constraint(op.f('mail_server_config_user_id_fkey'), 'mail_server_config', type_='foreignkey')
    op.drop_column('mail_server_config', 'user_id')
    op.alter_column('server_channel', 'webhook_token',
               existing_type=sa.VARCHAR(length=64),
               nullable=True)


def downgrade():
    # STRUCTURE ONLY — this restores the shape, not the contents.
    #
    # ``upgrade()`` drops a table and three columns, and dropping them destroys
    # every value they held. Nothing here recovers any of it: no
    # ``agent_email_integration`` row comes back, no ``mail_server_config``
    # gets its owner back, and no ``input_task`` gets its
    # ``source_agent_id`` / ``source_email_message_id`` back. What this
    # produces is an empty table and three empty columns.
    #
    # It also does not run to completion on a database that has been used.
    # Three ways it fails, in the order they are reached:
    #
    # 1. ``server_channel.webhook_token`` back to NOT NULL — the FIRST
    #    statement below, and the first to bite. Any polled channel has a NULL
    #    token (that is the whole point of this phase), so a single polled
    #    channel row fails the downgrade before it touches anything else.
    # 2. ``mail_server_config.user_id`` restored NOT NULL with no default —
    #    fails on any ``mail_server_config`` row. Honest outcome: the owner it
    #    would need is exactly the value ``upgrade()`` deleted, so the operator
    #    must decide who owns those rows.
    # 3. ``create_table('agent_email_integration')`` re-emits ``CREATE TYPE``
    #    for ``emailaccessmode`` and ``emailclonesharemode``. ``upgrade()``'s
    #    ``drop_table`` does not drop those enum types — PostgreSQL keeps them
    #    after their last column goes away — so this raises ``DuplicateObject``
    #    on a database that ran the upgrade. The upgrade is deliberately left
    #    as it is: no migration in this repo drops enum types, so it leaves the
    #    same two orphan types behind that every other one does, and making
    #    this one migration different would be the surprise.
    #
    # Each is fixable by hand (clear or backfill the offending rows; ``DROP
    # TYPE`` the two enums) — but this downgrade is not a one-command undo.
    op.alter_column('server_channel', 'webhook_token',
               existing_type=sa.VARCHAR(length=64),
               nullable=False)
    op.add_column('mail_server_config', sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False))
    op.create_foreign_key(op.f('mail_server_config_user_id_fkey'), 'mail_server_config', 'user', ['user_id'], ['id'], ondelete='CASCADE')
    op.add_column('input_task', sa.Column('source_email_message_id', sa.UUID(), autoincrement=False, nullable=True))
    op.add_column('input_task', sa.Column('source_agent_id', sa.UUID(), autoincrement=False, nullable=True))
    op.create_foreign_key(op.f('fk_input_task_source_email_message_id'), 'input_task', 'email_message', ['source_email_message_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_input_task_source_agent_id'), 'input_task', 'agent', ['source_agent_id'], ['id'], ondelete='SET NULL')
    op.create_table('agent_email_integration',
    sa.Column('enabled', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('access_mode', postgresql.ENUM('OPEN', 'RESTRICTED', name='emailaccessmode'), autoincrement=False, nullable=False),
    sa.Column('auto_approve_email_pattern', sa.VARCHAR(length=1024), autoincrement=False, nullable=True),
    sa.Column('allowed_domains', sa.VARCHAR(length=1024), autoincrement=False, nullable=True),
    sa.Column('max_clones', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('clone_share_mode', postgresql.ENUM('USER', 'BUILDER', name='emailclonesharemode'), autoincrement=False, nullable=False),
    sa.Column('incoming_mailbox', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('outgoing_from_address', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('agent_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('incoming_server_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('outgoing_server_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('agent_session_mode', sa.VARCHAR(), server_default=sa.text("'clone'::character varying"), autoincrement=False, nullable=False),
    sa.Column('process_as', sa.VARCHAR(), server_default=sa.text("'new_session'::character varying"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], name=op.f('agent_email_integration_agent_id_fkey'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['incoming_server_id'], ['mail_server_config.id'], name=op.f('agent_email_integration_incoming_server_id_fkey'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['outgoing_server_id'], ['mail_server_config.id'], name=op.f('agent_email_integration_outgoing_server_id_fkey'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('agent_email_integration_pkey')),
    sa.UniqueConstraint('agent_id', name=op.f('uq_agent_email_integration_agent_id'), postgresql_include=[], postgresql_nulls_not_distinct=False)
    )
