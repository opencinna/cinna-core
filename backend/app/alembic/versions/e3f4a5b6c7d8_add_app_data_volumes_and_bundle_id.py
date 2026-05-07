"""add app_data_volume table and Agent.bundle_id column

Revision ID: e3f4a5b6c7d8
Revises: ba8f1f14621f
Create Date: 2026-05-06 12:00:00.000000

Phase 1 of the Agent Bundles & Installs plan:

- Adds the ``app_data_volume`` table — per-user, per-bundle persistent
  storage records keyed by ``(user_id, bundle_id)``. Survives Install
  (Agent) deletion via the ``is_orphaned`` flag.

- Adds the ``bundle_id`` column to ``agent`` (reverse-DNS string, NOT NULL
  after backfill). Backfill runs in this migration: for every existing
  ``agent`` row we generate ``<reversed-host>.<short-uuid>`` using the
  same algorithm as ``BundleIdService.generate_bundle_id`` so the column
  matches what new agents will produce.

The ``bundle_uuid``, ``installed_revision_id``, ``is_publisher_install``
columns and the bundle CRUD tables land in Phase 2.

Downgrade drops both additions (preserves all data outside this scope).
"""
from alembic import op
import sqlalchemy as sa
from urllib.parse import urlparse


# revision identifiers, used by Alembic.
revision = 'e3f4a5b6c7d8'
down_revision = 'ba8f1f14621f'
branch_labels = None
depends_on = None


def _reversed_host_prefix() -> str:
    """Mirror of ``BundleIdService.reversed_host_prefix`` for the backfill.

    The migration must be self-contained (no app imports beyond settings),
    so we replicate the small parsing routine here. The two implementations
    are short enough that drift risk is acceptable; both are covered by
    the Phase 1 backend tests.
    """
    from app.core.config import settings

    raw = (settings.FRONTEND_HOST or "").strip()
    if not raw:
        return "localhost"
    parsed = urlparse(raw if "://" in raw else f"//{raw}", scheme="http")
    host = (parsed.hostname or raw).lower()
    parts = [p for p in host.split(".") if p]
    if not parts:
        return "localhost"
    return ".".join(reversed(parts))


def upgrade():
    # ── 1. Create app_data_volume ────────────────────────────────────
    op.create_table(
        'app_data_volume',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('bundle_id', sa.String(length=255), nullable=False),
        sa.Column('volume_name', sa.String(length=255), nullable=False),
        sa.Column('host_path', sa.String(length=1024), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('last_size_check_at', sa.DateTime(), nullable=True),
        sa.Column('current_install_id', sa.Uuid(), nullable=True),
        sa.Column('is_orphaned', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['current_install_id'], ['agent.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('volume_name', name='uq_app_data_volume_name'),
        sa.UniqueConstraint('user_id', 'bundle_id', name='uq_app_data_user_bundle'),
    )
    op.create_index(
        'ix_app_data_volume_user_id', 'app_data_volume', ['user_id'], unique=False
    )
    op.create_index(
        'ix_app_data_volume_bundle_id', 'app_data_volume', ['bundle_id'], unique=False
    )
    op.create_index(
        'ix_app_data_volume_orphaned',
        'app_data_volume',
        ['is_orphaned'],
        unique=False,
        postgresql_where=sa.text('is_orphaned = true'),
    )

    # ── 2. Add Agent.bundle_id (NULLABLE for backfill) ──────────────
    op.add_column(
        'agent',
        sa.Column('bundle_id', sa.String(length=255), nullable=True),
    )

    # ── 3. Backfill bundle_id from existing agent ids ───────────────
    # Each row gets ``<reversed-host>.<first-8-hex-of-uuid>`` — matches what
    # ``BundleIdService.generate_bundle_id`` will produce for new rows.
    prefix = _reversed_host_prefix()
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent "
            "SET bundle_id = :prefix || '.' || substring(replace(id::text, '-', '') from 1 for 8) "
            "WHERE bundle_id IS NULL"
        ),
        {"prefix": prefix},
    )

    # ── 4. Enforce NOT NULL + index ─────────────────────────────────
    op.alter_column('agent', 'bundle_id', nullable=False)
    op.create_index('ix_agent_bundle_id', 'agent', ['bundle_id'], unique=False)


def downgrade():
    # Reverse order: drop the Agent column then the table.
    op.drop_index('ix_agent_bundle_id', table_name='agent')
    op.drop_column('agent', 'bundle_id')

    op.drop_index('ix_app_data_volume_orphaned', table_name='app_data_volume')
    op.drop_index('ix_app_data_volume_bundle_id', table_name='app_data_volume')
    op.drop_index('ix_app_data_volume_user_id', table_name='app_data_volume')
    op.drop_table('app_data_volume')
