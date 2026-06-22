"""add user locale prefs and conversation style

Revision ID: 65d1ef4899be
Revises: a6f8dccba412
Create Date: 2026-06-22 18:30:23.630377

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '65d1ef4899be'
down_revision = 'a6f8dccba412'
branch_labels = None
depends_on = None


def upgrade():
    # Locale / communication preferences. The first three are free-text and
    # NULL when unset; ``conversation_style`` is NOT NULL with a server_default
    # so existing rows backfill to ``ai_default`` (current behavior) without a
    # separate data-migration pass.
    op.add_column('user', sa.Column('timezone', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('user', sa.Column('language', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('user', sa.Column('locale', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column(
        'user',
        sa.Column(
            'conversation_style',
            sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
            server_default='ai_default',
        ),
    )


def downgrade():
    op.drop_column('user', 'conversation_style')
    op.drop_column('user', 'locale')
    op.drop_column('user', 'language')
    op.drop_column('user', 'timezone')
