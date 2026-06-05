"""add discovered models to ai credential

Adds the per-credential model-discovery cache columns to ``ai_credential``.
Different API keys can see different models, so the available-model list is
cached per credential (populated by the model discovery cron):

- ``discovered_models``    JSON  — concrete provider model IDs the key can see;
                                   NULL = never discovered.
- ``models_discovered_at`` timestamptz — last SUCCESSFUL discovery timestamp.
- ``models_discovery_error`` text — coarse failure reason code (e.g.
                                   "oauth_token_unsupported"); NULL when healthy.

All three are nullable. No backfill — the cron populates them. Downgrade drops
all three.

Revision ID: 581dd9e44be1
Revises: c7e2a9f4b1d8
Create Date: 2026-06-05 12:04:09.772867

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '581dd9e44be1'
down_revision = 'c7e2a9f4b1d8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'ai_credential',
        sa.Column('discovered_models', sa.JSON(), nullable=True),
    )
    op.add_column(
        'ai_credential',
        sa.Column('models_discovered_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'ai_credential',
        sa.Column('models_discovery_error', sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column('ai_credential', 'models_discovery_error')
    op.drop_column('ai_credential', 'models_discovered_at')
    op.drop_column('ai_credential', 'discovered_models')
