"""add_skill_package_grants_and_plugin_support_flags

Two independent changes, deliberately in one migration because they belong to
one feature (agent addons) and splitting them would put two heads' worth of
ceremony in front of a single deploy:

1. ``skill_package_access_grant`` — the per-user allowlist behind a skill
   package's third visibility level, ``users``. A field-for-field mirror of
   ``bundle_access_grant``. ``skill_package.visibility`` itself needs no DDL:
   the column is a plain VARCHAR(16) and the level set is application-level,
   which is also why autogenerate cannot see that half of the change.

2. ``llm_plugin_marketplace_plugin.supported`` / ``unsupported_reason`` — the
   sync-time verdict on whether our containers can install a marketplace
   entry. Nothing writes them yet; the parsers that do arrive with the Codex
   and bare-skills marketplace formats. They are added now so that work needs
   no second migration against the same table.

DOWNGRADE WARNING — dropping ``skill_package_access_grant`` destroys every
share a publisher has handed out, and any package left at
``visibility='users'`` afterwards becomes invisible to everyone but its
publisher (the older ``user_can_see`` treats an unknown level as private).
Installs are unaffected: the container's archive download is authorised by the
install, never by visibility.

Revision ID: a3f1c07b9d42
Revises: b71f4a9c2d30
Create Date: 2026-09-08 16:20:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = 'a3f1c07b9d42'
down_revision = 'b71f4a9c2d30'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'skill_package_access_grant',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('package_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('granted_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['package_id'], ['skill_package.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        # SET NULL, not CASCADE: the granter leaving the instance must not
        # silently revoke the access they handed out.
        sa.ForeignKeyConstraint(
            ['granted_by_user_id'], ['user.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'package_id', 'user_id', name='uq_skill_grant_package_user'
        ),
    )
    op.create_index(
        'ix_skill_grant_package',
        'skill_package_access_grant',
        ['package_id'],
        unique=False,
    )
    op.create_index(
        'ix_skill_grant_user',
        'skill_package_access_grant',
        ['user_id'],
        unique=False,
    )

    # Existing marketplace entries were parsed by the Claude parser, which only
    # ever emitted installable rows — so the backfill default is `true`. The
    # server default is dropped afterwards: the value belongs to the parser
    # from here on, and a lingering default would quietly make an unparsed row
    # look supported.
    op.add_column(
        'llm_plugin_marketplace_plugin',
        sa.Column(
            'supported',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.alter_column(
        'llm_plugin_marketplace_plugin', 'supported', server_default=None
    )
    op.add_column(
        'llm_plugin_marketplace_plugin',
        sa.Column(
            'unsupported_reason',
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column('llm_plugin_marketplace_plugin', 'unsupported_reason')
    op.drop_column('llm_plugin_marketplace_plugin', 'supported')
    op.drop_index(
        'ix_skill_grant_user', table_name='skill_package_access_grant'
    )
    op.drop_index(
        'ix_skill_grant_package', table_name='skill_package_access_grant'
    )
    op.drop_table('skill_package_access_grant')
