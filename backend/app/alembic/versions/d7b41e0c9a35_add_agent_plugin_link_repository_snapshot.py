"""add_agent_plugin_link_repository_snapshot

``agent_plugin_link.snapshot_repository_url`` — the git URL an install was
actually made from — plus a backfill from the live marketplace rows.

WHY A THIRD SNAPSHOT COLUMN. ``c8d2e5b71a04`` backfilled the two *name*
snapshots so an orphaned marketplace link could still be named, and so a
returning entry could re-adopt it. That re-adoption matched on
``(snapshot_marketplace_name, snapshot_plugin_name)`` guarded by
``link.created_at >= marketplace.created_at``, and the guard covers exactly one
direction: a marketplace row *younger* than the link cannot be the one it was
installed from.

A rename goes the other way and slipped through. ``idx_marketplace_name_unique``
frees a name precisely when its holder is deleted — the event that orphans the
links in the first place — and ``sync_marketplace`` reassigns
``marketplace.name`` from the repository's own ``marketplace.json`` on every
sync. So an **older** marketplace row could acquire a deleted marketplace's
freed name, adopt its orphans, and from the next environment sync onward deliver
code from a repository the agent's owner never chose. The name backfill widened
that from "links created since the install path started writing names" to every
marketplace install in the database, which is why closing it belongs here and
not to a later release.

A repository URL is the identity a rename cannot transfer. From here on
``_reattach_orphaned_links`` requires it to match and **fails closed** on a NULL:
a link with no snapshot URL is never re-adopted.

BACKFILL SCOPE — the same join shape and the same ``source='marketplace'``
restriction as ``c8d2e5b71a04``, reading ``marketplace.url`` for links that
still point at a live plugin row. A link that is *already* orphaned has no live
row to read a URL from and gets none: it stays orphaned permanently, and its
remedy is uninstall-and-reinstall, which is the safe half of the trade. Inventing
a URL for it from a name is the exact confusion this column exists to end.

DOWNGRADE WARNING: dropping this column re-opens the rename hole described
above — the re-attach code that requires it is gone too at that point, so the
name-only match applies again. The values are true and cost nothing to keep; a
downgrade should be a schema rollback, never a cleanup.

Revision ID: d7b41e0c9a35
Revises: c8d2e5b71a04
Create Date: 2026-09-09 15:40:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = 'd7b41e0c9a35'
down_revision = 'c8d2e5b71a04'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_plugin_link',
        sa.Column(
            'snapshot_repository_url',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE agent_plugin_link AS link
        SET snapshot_repository_url = marketplace.url
        FROM llm_plugin_marketplace_plugin AS plugin
        JOIN llm_plugin_marketplace AS marketplace
          ON marketplace.id = plugin.marketplace_id
        WHERE link.plugin_id = plugin.id
          AND link.source = 'marketplace'
          AND link.snapshot_repository_url IS NULL
        """
    )


def downgrade():
    op.drop_column('agent_plugin_link', 'snapshot_repository_url')
