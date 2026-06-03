"""app_sync_pairing commit-then-reveal fields

Adds the commit-then-reveal hardening columns to ``app_sync_pairing``:
  - ``commitment``    (NOT NULL) — joiner key commitment, sent at ``start``.
  - ``sealer_nonce``  (nullable) — sealer's nonce, posted after the commitment.
  - ``joiner_nonce``  (nullable) — joiner's nonce, revealed last.

The relay stores these as opaque strings and never inspects them (zero-knowledge).

``commitment`` is NOT NULL. This is local-dev-only (nothing in production), so no
back-compat is required. We add it with ``server_default=''`` so the column can be
added to a possibly-non-empty dev table without error, then drop the default — new
rows are always written with a real commitment by the application layer.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = "a3c9e1f7b204"
down_revision = "3f8f2a2e7f23"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "app_sync_pairing",
        sa.Column(
            "commitment",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default="",
        ),
    )
    op.alter_column("app_sync_pairing", "commitment", server_default=None)
    op.add_column(
        "app_sync_pairing",
        sa.Column(
            "sealer_nonce", sqlmodel.sql.sqltypes.AutoString(), nullable=True
        ),
    )
    op.add_column(
        "app_sync_pairing",
        sa.Column(
            "joiner_nonce", sqlmodel.sql.sqltypes.AutoString(), nullable=True
        ),
    )


def downgrade():
    op.drop_column("app_sync_pairing", "joiner_nonce")
    op.drop_column("app_sync_pairing", "sealer_nonce")
    op.drop_column("app_sync_pairing", "commitment")
