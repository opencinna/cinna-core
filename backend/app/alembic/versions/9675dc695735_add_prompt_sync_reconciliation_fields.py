"""add prompt sync reconciliation fields

Revision ID: 9675dc695735
Revises: a3c9e1f7b204
Create Date: 2026-06-03 20:53:07.773059

Adds the prompt-sync reconciliation columns:
  - agent.{workflow,entrypoint,refiner}_prompt_updated_at — per-prompt logical
    clocks (LWW tiebreaker for the DB side), nullable, tz-aware.
  - agent_environment.{workflow,entrypoint,refiner}_prompt_synced_hash —
    per-environment last-reconciled content hash (the three-way merge base),
    nullable VARCHAR(64).

All columns are nullable with no default so existing rows backfill cleanly
(NULL = oldest clock / never reconciled).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9675dc695735'
down_revision = 'a3c9e1f7b204'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent', sa.Column('workflow_prompt_updated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent', sa.Column('entrypoint_prompt_updated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent', sa.Column('refiner_prompt_updated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_environment', sa.Column('workflow_prompt_synced_hash', sa.String(length=64), nullable=True))
    op.add_column('agent_environment', sa.Column('entrypoint_prompt_synced_hash', sa.String(length=64), nullable=True))
    op.add_column('agent_environment', sa.Column('refiner_prompt_synced_hash', sa.String(length=64), nullable=True))


def downgrade():
    op.drop_column('agent_environment', 'refiner_prompt_synced_hash')
    op.drop_column('agent_environment', 'entrypoint_prompt_synced_hash')
    op.drop_column('agent_environment', 'workflow_prompt_synced_hash')
    op.drop_column('agent', 'refiner_prompt_updated_at')
    op.drop_column('agent', 'entrypoint_prompt_updated_at')
    op.drop_column('agent', 'workflow_prompt_updated_at')
