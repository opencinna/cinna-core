"""resilient plugins: plugin source + bundle plugin_specs

Revision ID: 2ca38822e945
Revises: a7f3c9e2b1d4
Create Date: 2026-06-08 08:44:12.619352

Adds the plugin-source abstraction to ``agent_plugin_link`` and a
``plugin_specs`` snapshot column to ``agent_bundle_revision``.

Only plugin/bundle-related changes are included here — autogenerate also
surfaced pre-existing type drift on unrelated tables (ai_credential,
credential, mcp_connector, user_trusted_device) which is intentionally
excluded so this revision stays scoped to the resilient-plugin feature.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '2ca38822e945'
down_revision = 'a7f3c9e2b1d4'
branch_labels = None
depends_on = None


# Postgres auto-named the original FK when the column was created NOT NULL.
_FK_NAME = "agent_plugin_link_plugin_id_fkey"


def upgrade():
    # --- agent_bundle_revision: plugin specs snapshot -------------------
    op.add_column(
        'agent_bundle_revision',
        sa.Column(
            'plugin_specs',
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )

    # --- agent_plugin_link: plugin source + snapshot metadata ----------
    # Add ``source`` with a server_default so existing rows backfill to
    # 'marketplace' without a separate data migration. Default is retained
    # going forward (harmless; the ORM also supplies it).
    op.add_column(
        'agent_plugin_link',
        sa.Column(
            'source',
            sa.String(),
            server_default='marketplace',
            nullable=False,
        ),
    )
    op.add_column(
        'agent_plugin_link',
        sa.Column('snapshot_marketplace_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        'agent_plugin_link',
        sa.Column('snapshot_plugin_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        'agent_plugin_link',
        sa.Column('snapshot_config', sa.JSON(), nullable=True),
    )

    # plugin_id becomes nullable (bundle-sourced links carry NULL) with
    # ON DELETE SET NULL so deleting a marketplace plugin orphans the link
    # instead of cascading the install away.
    op.alter_column(
        'agent_plugin_link', 'plugin_id',
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.drop_constraint(_FK_NAME, 'agent_plugin_link', type_='foreignkey')
    op.create_foreign_key(
        _FK_NAME,
        'agent_plugin_link',
        'llm_plugin_marketplace_plugin',
        ['plugin_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    # Restore ON DELETE CASCADE + NOT NULL on plugin_id. Only safe when no
    # bundle-sourced rows (plugin_id NULL) exist; otherwise the NOT NULL
    # alter will fail, which is the intended guard.
    op.drop_constraint(_FK_NAME, 'agent_plugin_link', type_='foreignkey')
    op.create_foreign_key(
        _FK_NAME,
        'agent_plugin_link',
        'llm_plugin_marketplace_plugin',
        ['plugin_id'], ['id'],
        ondelete='CASCADE',
    )
    op.alter_column(
        'agent_plugin_link', 'plugin_id',
        existing_type=sa.UUID(),
        nullable=False,
    )

    op.drop_column('agent_plugin_link', 'snapshot_config')
    op.drop_column('agent_plugin_link', 'snapshot_plugin_name')
    op.drop_column('agent_plugin_link', 'snapshot_marketplace_name')
    op.drop_column('agent_plugin_link', 'source')
    op.drop_column('agent_bundle_revision', 'plugin_specs')
