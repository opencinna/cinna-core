"""credential_oauth_state: pending Google OAuth authorization states in the DB

Replaces the in-process ``_oauth_states`` dict so an authorize and its callback
may land on different workers. Stores only ``sha256(state)``.

Revision ID: 90a3fecdf28b
Revises: 31f1c9c4a952
Create Date: 2026-10-17 18:50:41.511182

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '90a3fecdf28b'
down_revision = '31f1c9c4a952'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('credential_oauth_state',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('state_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('credential_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('origin', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['credential_id'], ['credential.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_credential_oauth_state_credential_id'), 'credential_oauth_state', ['credential_id'], unique=False)
    op.create_index(op.f('ix_credential_oauth_state_expires_at'), 'credential_oauth_state', ['expires_at'], unique=False)
    op.create_index(op.f('ix_credential_oauth_state_state_hash'), 'credential_oauth_state', ['state_hash'], unique=True)


def downgrade():
    op.drop_index(op.f('ix_credential_oauth_state_state_hash'), table_name='credential_oauth_state')
    op.drop_index(op.f('ix_credential_oauth_state_expires_at'), table_name='credential_oauth_state')
    op.drop_index(op.f('ix_credential_oauth_state_credential_id'), table_name='credential_oauth_state')
    op.drop_table('credential_oauth_state')
