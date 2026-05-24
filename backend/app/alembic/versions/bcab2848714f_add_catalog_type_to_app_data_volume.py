"""add catalog_type discriminator to app_data_volume

Revision ID: bcab2848714f
Revises: 538667612c3d
Create Date: 2026-05-23 12:00:00.000000

Splits the per-(user, bundle) unique constraint on ``app_data_volume`` into
a per-(user, bundle, catalog_type) constraint so the publisher's working
copy of a bundle (publisher install) and a consumer install of the same
bundle by the same user can coexist as separate volumes.

Schema changes:

- Add ``catalog_type`` (``VARCHAR(64) NULL``). Plain string column —
  open-ended on purpose so future sources (``"marketplace"``,
  ``"remote:<host>"``) don't need an enum migration.
- Drop ``uq_app_data_user_bundle`` (``(user_id, bundle_id)``).
- Add ``uq_app_data_user_bundle_catalog`` (``(user_id, bundle_id, catalog_type)``).
  Postgres treats NULLs as distinct in unique constraints, which is what
  we want — a NULL publisher slot coexists with a ``"server"`` consumer
  slot for the same ``(user, bundle)``.

Backfill rules (run inside the same migration):

- For each existing ``app_data_volume`` row, look at the paired ``agent``
  row via ``current_install_id``:
    - paired agent has ``bundle_uuid IS NULL`` (unpublished standalone
      agent, will become a publisher install on first publish) OR
      ``is_publisher_install = TRUE`` → ``catalog_type = NULL``
    - paired agent has ``bundle_uuid != NULL`` AND
      ``is_publisher_install = FALSE`` (consumer install) →
      ``catalog_type = 'server'``
- For orphan rows (``current_install_id IS NULL`` — agent already deleted,
  FK SET NULL ran), default to ``"server"``. We can't recover the original
  slot of a stranded orphan after the paired agent row is gone; defaulting
  to ``"server"`` reflects the more common case (consumer installs vastly
  outnumber publisher installs in steady state).

Downgrade restores the original ``(user_id, bundle_id)`` unique
constraint. Before doing that we collapse any duplicates by deleting all
rows whose ``catalog_type IS NOT NULL`` (i.e. the new consumer-install
rows that the old schema couldn't represent) — the publisher's NULL row
survives. This is destructive, but the downgrade only makes sense as a
rollback before consumer installs were widely used.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bcab2848714f'
down_revision = '538667612c3d'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Add the column (nullable, no server default). ────────────
    op.add_column(
        'app_data_volume',
        sa.Column('catalog_type', sa.String(length=64), nullable=True),
    )

    # ── 2. Backfill from the paired Agent row. ──────────────────────
    # Slot policy (must match the runtime rule in
    # ``EnvironmentLifecycleManager._resolve_app_data_host_path``):
    #   - unpublished standalone agent (``bundle_uuid IS NULL``) OR
    #     publisher install (``is_publisher_install = TRUE``) → NULL
    #   - everything else (consumer install) → ``'server'``
    # Orphans (no paired agent row) fall to ``'server'`` — we can't
    # determine the original slot once the paired agent is gone, and
    # consumer installs dominate the steady-state population.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE app_data_volume v
            SET catalog_type = CASE
                WHEN a.bundle_uuid IS NULL OR a.is_publisher_install = TRUE
                    THEN NULL
                ELSE 'server'
            END
            FROM agent a
            WHERE v.current_install_id = a.id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE app_data_volume
            SET catalog_type = 'server'
            WHERE current_install_id IS NULL
              AND catalog_type IS NULL
            """
        )
    )

    # ── 3. Swap the unique constraint. ──────────────────────────────
    op.drop_constraint(
        'uq_app_data_user_bundle', 'app_data_volume', type_='unique'
    )
    op.create_unique_constraint(
        'uq_app_data_user_bundle_catalog',
        'app_data_volume',
        ['user_id', 'bundle_id', 'catalog_type'],
    )


def downgrade():
    # Drop the new constraint first.
    op.drop_constraint(
        'uq_app_data_user_bundle_catalog',
        'app_data_volume',
        type_='unique',
    )

    # Collapse duplicates that the old schema can't represent. Prefer
    # keeping the publisher-slot row (catalog_type IS NULL) — it's the
    # one whose lifecycle the publisher controls. Any non-NULL slot
    # (consumer installs) gets dropped.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM app_data_volume
            WHERE catalog_type IS NOT NULL
              AND (user_id, bundle_id) IN (
                  SELECT user_id, bundle_id
                  FROM app_data_volume
                  GROUP BY user_id, bundle_id
                  HAVING COUNT(*) > 1
              )
            """
        )
    )

    op.create_unique_constraint(
        'uq_app_data_user_bundle',
        'app_data_volume',
        ['user_id', 'bundle_id'],
    )

    op.drop_column('app_data_volume', 'catalog_type')
