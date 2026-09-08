"""add_agent_plugin_link_catalog_source

Adds ``agent_plugin_link.skill_package_revision_id`` — the pinned skill
package revision behind a ``source='catalog'`` link, and the signal the
Plugins tab reads for "an update is available" (``package.latest_revision_id``
differs from it).

``source`` itself is untouched: the column is a plain VARCHAR and
``PluginSource`` is an application-level enum, so the new ``catalog`` value
needs no DDL. That is also why autogenerate cannot see this change and the
migration is written by hand.

DOWNGRADE WARNING — this migration drops the only column that identifies which
catalog revision a link was installed from. Before downgrading, an operator
must first rewrite or delete every ``agent_plugin_link`` row with
``source = 'catalog'``: without the FK those rows describe a plugin the
container cannot fetch, and the manifest builder would emit entries with no
archive coordinates. The downgrade deliberately does not delete them for you —
silently removing somebody's installed skills is not a schema operation.

Revision ID: b71f4a9c2d30
Revises: c24bd7ff8728
Create Date: 2026-09-08 14:35:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b71f4a9c2d30'
down_revision = 'c24bd7ff8728'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_plugin_link',
        sa.Column('skill_package_revision_id', sa.Uuid(), nullable=True),
    )
    op.create_index(
        op.f('ix_agent_plugin_link_skill_package_revision_id'),
        'agent_plugin_link',
        ['skill_package_revision_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_agent_plugin_link_skill_package_revision',
        'agent_plugin_link',
        'skill_package_revision',
        ['skill_package_revision_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_agent_plugin_link_skill_package_revision',
        'agent_plugin_link',
        type_='foreignkey',
    )
    op.drop_index(
        op.f('ix_agent_plugin_link_skill_package_revision_id'),
        table_name='agent_plugin_link',
    )
    op.drop_column('agent_plugin_link', 'skill_package_revision_id')
