"""agent_bundle_revision agent-row definitional metadata columns

Widens the shared revision snapshot (used by both bundle publish/install and git
checkout/push/pull) so agent-row definitional fields survive a round-trip:
``description``, ``example_prompts``, ``status_refresh_command``,
``agent_api_enabled``, ``agent_api_identity_enabled``, ``a2a_config``,
``agent_sdk_config`` and ``webapp_enabled``.

All columns are nullable with no server-side backfill: a NULL means "this
snapshot did not carry the field", which the restore side treats as
"do not overwrite the consumer's current value" (missing-key-tolerant). Existing
rows and pre-existing v1/v2 manifests therefore read cleanly.

Revision ID: d9b3e1a7c45f
Revises: c8a4f1e09b27
Create Date: 2026-06-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'd9b3e1a7c45f'
down_revision = 'c8a4f1e09b27'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent_bundle_revision',
        sa.Column('description', sa.Text(), nullable=True),
    )
    op.add_column(
        'agent_bundle_revision',
        sa.Column('example_prompts', postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'agent_bundle_revision',
        sa.Column('status_refresh_command', sa.String(length=1024), nullable=True),
    )
    op.add_column(
        'agent_bundle_revision',
        sa.Column('agent_api_enabled', sa.Boolean(), nullable=True),
    )
    op.add_column(
        'agent_bundle_revision',
        sa.Column('agent_api_identity_enabled', sa.Boolean(), nullable=True),
    )
    op.add_column(
        'agent_bundle_revision',
        sa.Column('a2a_config', postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'agent_bundle_revision',
        sa.Column('agent_sdk_config', postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'agent_bundle_revision',
        sa.Column('webapp_enabled', sa.Boolean(), nullable=True),
    )


def downgrade():
    op.drop_column('agent_bundle_revision', 'webapp_enabled')
    op.drop_column('agent_bundle_revision', 'agent_sdk_config')
    op.drop_column('agent_bundle_revision', 'a2a_config')
    op.drop_column('agent_bundle_revision', 'agent_api_identity_enabled')
    op.drop_column('agent_bundle_revision', 'agent_api_enabled')
    op.drop_column('agent_bundle_revision', 'status_refresh_command')
    op.drop_column('agent_bundle_revision', 'example_prompts')
    op.drop_column('agent_bundle_revision', 'description')
