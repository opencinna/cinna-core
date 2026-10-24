"""plugin snapshots: runs_code also covers commands / agents that run shell

Data only. Re-analyses every ready claude / codex snapshot from its stored
archive (no re-fetch: a resync never re-reads an unchanged source key) and
flips ``runs_code`` false -> true where a command or agent runs shell. A
missing archive is skipped; the addons directory version is bumped when any
row changed. Idempotent.

Revision ID: 5b7e2f9c1a3d
Revises: 48c3eee7b6cd
Create Date: 2026-10-24 18:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '5b7e2f9c1a3d'
down_revision = '48c3eee7b6cd'
branch_labels = None
depends_on = None


def upgrade():
    from app.services.plugins.plugin_runs_code_backfill import backfill_runs_code

    backfill_runs_code(op.get_bind())


def downgrade():
    # The old verdicts are not kept; a stricter runs_code is harmless.
    pass
