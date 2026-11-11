"""voice transcription relay storage

Server-wide voice transcription settings on ``server_config`` (enabled flag,
speech provider, per-user monthly minutes cap) and the per-session usage
ledger ``voice_transcription_usage``. The provider FK is ON DELETE SET NULL,
never RESTRICT: deleting a provider is gated in the service layer, not by the
database.

Revision ID: 46e56c924dd3
Revises: f725f8313224
Create Date: 2026-11-11 07:42:32.374858

"""
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision = '46e56c924dd3'
down_revision = 'f725f8313224'
branch_labels = None
depends_on = None

PROVIDER_FK = 'fk_server_config_voice_transcription_provider_id'


def upgrade():
    op.create_table('voice_transcription_usage',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('audio_seconds', sa.Float(), nullable=False),
    sa.Column('replay_seconds', sa.Float(), nullable=False),
    sa.Column('provider_id', sa.Uuid(), nullable=True),
    sa.Column('provider_type', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=True),
    sa.Column('model', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
    sa.Column('client_kind', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('outcome', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['ai_provider.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id')
    )
    op.create_index('ix_voice_transcription_usage_user_started', 'voice_transcription_usage', ['user_id', 'started_at'], unique=False)

    op.add_column('server_config', sa.Column('voice_transcription_enabled', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('server_config', sa.Column('voice_transcription_provider_id', sa.Uuid(), nullable=True))
    op.add_column('server_config', sa.Column('voice_transcription_monthly_minutes_per_user', sa.Integer(), nullable=True))
    op.create_foreign_key(PROVIDER_FK, 'server_config', 'ai_provider', ['voice_transcription_provider_id'], ['id'], ondelete='SET NULL')


def downgrade():
    op.drop_constraint(PROVIDER_FK, 'server_config', type_='foreignkey')
    op.drop_column('server_config', 'voice_transcription_monthly_minutes_per_user')
    op.drop_column('server_config', 'voice_transcription_provider_id')
    op.drop_column('server_config', 'voice_transcription_enabled')

    op.drop_index('ix_voice_transcription_usage_user_started', table_name='voice_transcription_usage')
    op.drop_table('voice_transcription_usage')
