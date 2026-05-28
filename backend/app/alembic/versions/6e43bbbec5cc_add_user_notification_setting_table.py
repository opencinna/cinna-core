"""add user_notification_setting table

Revision ID: 6e43bbbec5cc
Revises: aa44agentapi04
Create Date: 2026-05-27 10:02:04.057322

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '6e43bbbec5cc'
down_revision = 'aa44agentapi04'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_notification_setting',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('notification_type', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('email_enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'notification_type', name='uq_user_notification_setting_user_type'),
    )
    op.create_index('ix_user_notification_setting_user_id', 'user_notification_setting', ['user_id'], unique=False)


def downgrade():
    op.drop_index('ix_user_notification_setting_user_id', table_name='user_notification_setting')
    op.drop_table('user_notification_setting')
