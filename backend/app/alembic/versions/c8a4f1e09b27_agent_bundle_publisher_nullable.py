"""agent_bundle.publisher_user_id nullable (ownerless git-imported bundles)

Git-sourced bundles are imported as ownerless / shared rows
(``publisher_user_id = NULL``) so one bundle row backs every user's checkout of
the same repo — the checking-out user is not the publisher. A catalog publish
still sets a real publisher. This only relaxes the NOT NULL constraint; the FK
(ondelete RESTRICT) is unchanged.

Revision ID: c8a4f1e09b27
Revises: 391a6285d8ff
Create Date: 2026-06-27 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c8a4f1e09b27'
down_revision = '391a6285d8ff'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'agent_bundle',
        'publisher_user_id',
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade():
    # Ownerless rows (git imports) must be removed before re-imposing NOT NULL,
    # otherwise the constraint cannot be applied.
    op.execute("DELETE FROM agent_bundle WHERE publisher_user_id IS NULL")
    op.alter_column(
        'agent_bundle',
        'publisher_user_id',
        existing_type=sa.Uuid(),
        nullable=False,
    )
