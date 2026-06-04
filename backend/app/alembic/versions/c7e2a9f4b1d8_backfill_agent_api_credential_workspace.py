"""backfill agent_api credential user_workspace_id

Data-only migration for Problem 1 of the agent-api-automatic-credentials
feature. Legacy ``agent_api`` credentials were created before the connect
helper stamped ``user_workspace_id``, so they sit in the default (NULL)
workspace and disappear under any non-default workspace filter.

Backfill rule (conservative — only when unambiguous):
  - credential.type = 'agent_api'
  - credential.user_workspace_id IS NULL
  - the credential is linked to exactly ONE agent
  - that agent has a non-NULL user_workspace_id
→ stamp the credential with that agent's workspace.

Leaves NULL when ambiguous (zero or multiple links, or the linked agent is
in the default workspace). Downgrade is a no-op — the original NULLs cannot
be reliably reconstructed and the change is purely a grouping convenience.

Revision ID: c7e2a9f4b1d8
Revises: 1a43b403f066
Create Date: 2026-06-04 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "c7e2a9f4b1d8"
down_revision = "1a43b403f066"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE credential AS c
        SET user_workspace_id = sub.user_workspace_id
        FROM (
            -- One linked agent (with a workspace) per credential. ``MIN`` over
            -- the uuid::text cast satisfies Postgres' lack of MIN(uuid); the
            -- HAVING clause guarantees a single distinct value anyway.
            SELECT
                l.credential_id AS credential_id,
                MIN(a.user_workspace_id::text)::uuid AS user_workspace_id
            FROM agent_credential_link AS l
            JOIN agent AS a ON a.id = l.agent_id
            WHERE a.user_workspace_id IS NOT NULL
            GROUP BY l.credential_id
            HAVING COUNT(DISTINCT l.agent_id) = 1
        ) AS sub
        WHERE c.id = sub.credential_id
          AND c.type = 'AGENT_API'
          AND c.user_workspace_id IS NULL
        """
    )


def downgrade():
    # No-op: the original NULL workspace cannot be reliably restored, and the
    # stamp is a presentational grouping convenience, not an auth boundary.
    pass
