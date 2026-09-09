"""backfill_marketplace_link_name_snapshots

Data only — no schema change.

``agent_plugin_link.snapshot_marketplace_name`` / ``snapshot_plugin_name`` are
the *directory* identity of an install: ``plugins/<marketplace>/<plugin>/``.
Bundle and catalog installs have always written them; the marketplace install
path did not, because a marketplace link resolves its names from the live
plugin row instead.

That works right up to the moment the live row disappears. ``plugin_id`` is
``ON DELETE SET NULL`` so an admin deleting a marketplace — or an upstream repo
dropping an entry on the next sync — orphans the link instead of uninstalling
somebody's plugin. Without these two columns such a link has no name left: it
renders as an unknown plugin and the skills the environment still loads from
its directory land on a *second*, synthetic row. One directory, two rows,
neither of them nameable.

The install path now writes both columns. This backfills every link already in
the database from the plugin and marketplace rows it still points at, so
existing installs get the same protection instead of only ones created from
here on. A link whose plugin row is *already* gone cannot be reconstructed —
there is nothing left to read the names from — and is left as it is.

DOWNGRADE: no-op. The columns exist either way and the values are true; erasing
them would only re-create the defect this fixes.

Revision ID: c8d2e5b71a04
Revises: a3f1c07b9d42
Create Date: 2026-09-09 10:15:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c8d2e5b71a04'
down_revision = 'a3f1c07b9d42'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE agent_plugin_link AS link
        SET snapshot_marketplace_name = marketplace.name,
            snapshot_plugin_name = plugin.name
        FROM llm_plugin_marketplace_plugin AS plugin
        JOIN llm_plugin_marketplace AS marketplace
          ON marketplace.id = plugin.marketplace_id
        WHERE link.plugin_id = plugin.id
          AND link.source = 'marketplace'
          AND (
            link.snapshot_marketplace_name IS NULL
            OR link.snapshot_plugin_name IS NULL
          )
        """
    )


def downgrade():
    # Deliberately nothing: see the module docstring.
    pass
