"""add agent api issued key columns

Agent API Issued Keys (plan ``docs/plans/agent_api_issued_keys_plan.md`` §1, §2):

- Soft revoke + rotation columns on ``agent_api_token``: ``revoked_at``,
  ``revoked_reason``, ``previous_token_hash`` (+ index), ``previous_token_expires_at``,
  ``rotated_at``, ``issuer_saw_value``.
- ``agent_id`` and ``owner_id`` become NULLABLE with ``ON DELETE SET NULL`` so a
  key survives its producer / issuer being deleted; the status is derived from
  the NULL (``producer_removed`` / ``issuer_removed``).
- Data (D12): an external key issued to someone else moves to that person —
  credential ownership transfers to ``subject_user_id``, issuer-side agent links
  and shares are dropped, the workspace is cleared, ``issuer_saw_value`` is set.
- Data (D23): external-key credentials with no ``service_uri`` get
  ``<slug(producer name)>-api`` (same rule as ``AgentApiKeyService.default_service_uri``).

The moved keys stay in the issuer's running containers' credentials.json until
those environments next sync (the issuer-side links are gone, so that sync
removes them); nothing is pushed by this migration.

Downgrade is partial by design: the ownership transfer and the ``service_uri``
backfill are NOT reversed. Rows whose producer or issuer is gone (NULL
``agent_id`` / ``owner_id``) are deleted so NOT NULL + CASCADE can be restored.

Revision ID: 49c748e2c50a
Revises: e5a9c3d7f2b4
Create Date: 2026-10-07 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '49c748e2c50a'
down_revision = 'e5a9c3d7f2b4'
branch_labels = None
depends_on = None


_ISSUED_TO_OTHER = (
    "t.kind = 'external' AND t.subject_user_id IS NOT NULL "
    "AND t.subject_user_id <> t.owner_id"
)


def upgrade():
    # 1. Columns.
    op.add_column(
        'agent_api_token',
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'agent_api_token',
        sa.Column('revoked_reason', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'agent_api_token',
        sa.Column('previous_token_hash', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'agent_api_token',
        sa.Column(
            'previous_token_expires_at', sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        'agent_api_token',
        sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'agent_api_token',
        sa.Column(
            'issuer_saw_value',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        'ix_agent_api_token_previous_token_hash',
        'agent_api_token',
        ['previous_token_hash'],
        unique=False,
    )

    # 2. agent_id / owner_id: nullable + ON DELETE SET NULL.
    op.drop_constraint(
        'agent_api_token_agent_id_fkey', 'agent_api_token', type_='foreignkey'
    )
    op.drop_constraint(
        'agent_api_token_owner_id_fkey', 'agent_api_token', type_='foreignkey'
    )
    op.alter_column('agent_api_token', 'agent_id', existing_type=sa.Uuid(), nullable=True)
    op.alter_column('agent_api_token', 'owner_id', existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        'agent_api_token_agent_id_fkey',
        'agent_api_token',
        'agent',
        ['agent_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'agent_api_token_owner_id_fkey',
        'agent_api_token',
        'user',
        ['owner_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # 3. Data (D12, D23). One statement each.
    op.execute(
        "UPDATE credential c SET owner_id = t.subject_user_id, user_workspace_id = NULL "
        f"FROM agent_api_token t WHERE t.credential_id = c.id AND {_ISSUED_TO_OTHER}"
    )
    op.execute(
        "DELETE FROM agent_credential_link l USING agent_api_token t, agent a "
        "WHERE l.credential_id = t.credential_id AND l.agent_id = a.id "
        "AND t.kind = 'external' AND a.owner_id <> t.subject_user_id"
    )
    op.execute(
        "DELETE FROM credential_shares s USING agent_api_token t "
        f"WHERE s.credential_id = t.credential_id AND {_ISSUED_TO_OTHER}"
    )
    op.execute(
        "UPDATE agent_api_token t SET issuer_saw_value = true "
        f"WHERE {_ISSUED_TO_OTHER}"
    )
    op.execute(
        "UPDATE credential c SET service_uri = "
        "coalesce(nullif(trim(both '-' from left(trim(both '-' from "
        "regexp_replace(lower(a.name), '[^a-z0-9]+', '-', 'g')), 40)), ''), 'agent') "
        "|| '-api' "
        "FROM agent_api_token t, agent a "
        "WHERE t.credential_id = c.id AND t.agent_id = a.id "
        "AND t.kind = 'external' AND c.service_uri IS NULL"
    )


def downgrade():
    # Ownership transfer and service_uri backfill are one-way (not reversed).
    op.execute("DELETE FROM agent_api_token WHERE agent_id IS NULL OR owner_id IS NULL")

    op.drop_constraint(
        'agent_api_token_owner_id_fkey', 'agent_api_token', type_='foreignkey'
    )
    op.drop_constraint(
        'agent_api_token_agent_id_fkey', 'agent_api_token', type_='foreignkey'
    )
    op.alter_column('agent_api_token', 'owner_id', existing_type=sa.Uuid(), nullable=False)
    op.alter_column('agent_api_token', 'agent_id', existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        'agent_api_token_owner_id_fkey',
        'agent_api_token',
        'user',
        ['owner_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'agent_api_token_agent_id_fkey',
        'agent_api_token',
        'agent',
        ['agent_id'],
        ['id'],
        ondelete='CASCADE',
    )

    op.drop_index('ix_agent_api_token_previous_token_hash', table_name='agent_api_token')
    op.drop_column('agent_api_token', 'issuer_saw_value')
    op.drop_column('agent_api_token', 'rotated_at')
    op.drop_column('agent_api_token', 'previous_token_expires_at')
    op.drop_column('agent_api_token', 'previous_token_hash')
    op.drop_column('agent_api_token', 'revoked_reason')
    op.drop_column('agent_api_token', 'revoked_at')
