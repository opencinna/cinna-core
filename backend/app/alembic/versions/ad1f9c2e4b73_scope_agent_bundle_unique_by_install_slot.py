"""scope agent bundle unique constraint by install slot

Revision ID: ad1f9c2e4b73
Revises: bcab2848714f
Create Date: 2026-05-23 14:00:00.000000

The agent table previously enforced
``uq_agent_bundle_id_per_publisher`` on ``(owner_id, bundle_id)``, which
made it impossible for a single user to own both a publisher install
(``is_publisher_install=True``) and a separate consumer install
(``is_publisher_install=False``) of the same bundle.

The install-experience-redesign feature requires exactly that: the
publisher must be able to install their own bundle as a separate
consumer copy so "Open" from the catalog launches the consumer agent
rather than dumping them into their dev / source copy.

This migration replaces the constraint with a composite key that
includes the install slot. Postgres treats a boolean column as a normal
column for uniqueness purposes, so ``(owner, bundle, true)`` and
``(owner, bundle, false)`` are distinct — exactly what we want.

The existing partial unique index
``uq_agent_publisher_install_per_bundle`` (one publisher install per
bundle, scoped by ``is_publisher_install = true``) is untouched.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'ad1f9c2e4b73'
down_revision = 'bcab2848714f'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        'uq_agent_bundle_id_per_publisher', 'agent', type_='unique'
    )
    op.create_unique_constraint(
        'uq_agent_bundle_id_per_publisher',
        'agent',
        ['owner_id', 'bundle_id', 'is_publisher_install'],
    )


def downgrade():
    # Restore the original 2-column constraint. Before doing so we must
    # collapse duplicates that the old shape can't represent — for any
    # (owner, bundle) pair that has both a publisher and a consumer
    # install row, drop the consumer install. The publisher install is
    # the dev / source copy and is not user-deletable, so it's the safer
    # row to keep.
    op.execute(
        """
        DELETE FROM agent
        WHERE is_publisher_install = FALSE
          AND (owner_id, bundle_id) IN (
              SELECT owner_id, bundle_id
              FROM agent
              GROUP BY owner_id, bundle_id
              HAVING COUNT(*) > 1
          )
        """
    )

    op.drop_constraint(
        'uq_agent_bundle_id_per_publisher', 'agent', type_='unique'
    )
    op.create_unique_constraint(
        'uq_agent_bundle_id_per_publisher',
        'agent',
        ['owner_id', 'bundle_id'],
    )
