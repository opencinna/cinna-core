"""remove app agent routes and enforce singleton app mcp channel

Revision ID: 867cacb5a827
Revises: 907124e812c5
Create Date: 2026-08-25 08:05:00.698775

Channels & identity unification, phase 5. Three changes:

1. Drop the whole ``AppAgentRoute`` family. Routing now runs on the agents a
   caller owns plus their enabled identity bindings; there is no route table
   left to consult.
2. Drop ``identity_agent_binding.message_patterns``. Glob pre-matching was
   retired in phase 1; this removes the storage behind it.
3. Add a partial unique index making ``channel_type = 'app_mcp'`` a singleton
   at the database level.

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '867cacb5a827'
down_revision = '907124e812c5'
branch_labels = None
depends_on = None


# Partial unique index enforcing "at most one row per singleton channel type".
#
# The predicate is a SQL literal, so the singleton list is hardcoded here
# rather than read from ``registry.singleton_channel_types()``. Adding a
# future singleton transport therefore needs a NEW migration that rebuilds
# this index with the extra type in the IN-list. That is deliberate: an index
# predicate cannot call Python, and a blanket unique index on
# ``channel_type`` would be wrong — ``google_chat`` and ``email`` may
# legitimately have many rows each.
SINGLETON_TYPE_INDEX = "uq_server_channel_singleton_type"
SINGLETON_TYPE_PREDICATE = "channel_type IN ('app_mcp')"


def upgrade():
    # --- 1. Drop the AppAgentRoute family -------------------------------
    #
    # Order is load-bearing: ``app_agent_route_assignment.route_id`` carries an
    # FK to ``app_agent_route.id``, so the child table goes first. PostgreSQL
    # refuses to drop the parent while the dependent constraint exists.
    # ``user_app_agent_route`` is independent of both.
    op.drop_index(
        op.f('ix_app_agent_route_assignment_user_id'),
        table_name='app_agent_route_assignment',
    )
    op.drop_table('app_agent_route_assignment')

    op.drop_index(op.f('ix_app_agent_route_agent_id'), table_name='app_agent_route')
    op.drop_index(op.f('ix_app_agent_route_created_by'), table_name='app_agent_route')
    op.drop_table('app_agent_route')

    op.drop_index(
        op.f('ix_user_app_agent_route_user_id'), table_name='user_app_agent_route'
    )
    op.drop_table('user_app_agent_route')

    # --- 2. Drop the retired pattern-matching column ---------------------
    op.drop_column('identity_agent_binding', 'message_patterns')

    # --- 3. Enforce singleton channel types ------------------------------
    #
    # This is a SECOND guard, not a replacement for the first.
    # ``ServerChannel`` also carries ``UniqueConstraint("name")``
    # (``uq_server_channel_name``), and that constraint is load-bearing in a
    # non-obvious way: ``ServerChannelService.get_or_create_singleton`` handles
    # the two-worker materialization race by catching ``IntegrityError``,
    # rolling back and re-reading the winner's row. Both racing workers compute
    # the same ``_singleton_name``, so the loser collides on the NAME — that is
    # the collision the retry path is written against.
    #
    # DO NOT drop or relax ``uq_server_channel_name`` as "now redundant"
    # because of the index below. Without it, two concurrent materializations
    # of the same singleton type stop colliding on name; they would still be
    # caught here, but the constraint is what the documented race path names.
    # Retiring it silently degrades a guarded race into an unguarded one.
    op.create_index(
        SINGLETON_TYPE_INDEX,
        'server_channel',
        ['channel_type'],
        unique=True,
        postgresql_where=sa.text(SINGLETON_TYPE_PREDICATE),
    )


def downgrade():
    # Reversible in SHAPE ONLY. The route rows and the ``message_patterns``
    # values are destroyed by ``upgrade()`` and cannot be restored here —
    # downgrading gives you empty tables and a NULL column, not your data back.
    # Column definitions below mirror the live pre-drop schema (i.e. the
    # creating migration bccf5d92996f plus the columns later migrations added
    # to ``app_agent_route``), not just the original CREATE TABLE.
    op.drop_index(SINGLETON_TYPE_INDEX, table_name='server_channel')

    op.add_column(
        'identity_agent_binding',
        sa.Column('message_patterns', sa.TEXT(), autoincrement=False, nullable=True),
    )

    op.create_table('user_app_agent_route',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('agent_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('session_mode', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('trigger_prompt', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('message_patterns', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('is_active', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('channel_app_mcp', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], name=op.f('user_app_agent_route_agent_id_fkey'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('user_app_agent_route_user_id_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('user_app_agent_route_pkey')),
    sa.UniqueConstraint('user_id', 'agent_id', name=op.f('uq_user_app_agent_route'))
    )
    op.create_index(op.f('ix_user_app_agent_route_user_id'), 'user_app_agent_route', ['user_id'], unique=False)

    # Parent before child: ``app_agent_route_assignment.route_id`` references
    # ``app_agent_route.id``, so the route table must exist first. This is the
    # mirror image of the drop order in ``upgrade()``.
    op.create_table('app_agent_route',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('agent_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('session_mode', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('trigger_prompt', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('message_patterns', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('is_active', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_by', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.Column('auto_enable_for_users', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('prompt_examples', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('is_auto_managed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('channel_app_mcp', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], name=op.f('app_agent_route_agent_id_fkey'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], name=op.f('app_agent_route_created_by_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('app_agent_route_pkey'))
    )
    op.create_index(op.f('ix_app_agent_route_agent_id'), 'app_agent_route', ['agent_id'], unique=False)
    op.create_index(op.f('ix_app_agent_route_created_by'), 'app_agent_route', ['created_by'], unique=False)

    op.create_table('app_agent_route_assignment',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('route_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('is_enabled', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['route_id'], ['app_agent_route.id'], name=op.f('app_agent_route_assignment_route_id_fkey'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('app_agent_route_assignment_user_id_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('app_agent_route_assignment_pkey')),
    sa.UniqueConstraint('route_id', 'user_id', name=op.f('uq_app_agent_route_assignment'))
    )
    op.create_index(op.f('ix_app_agent_route_assignment_user_id'), 'app_agent_route_assignment', ['user_id'], unique=False)
