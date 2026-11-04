"""add_user_dashboard_origin

Dashboards D6: ``user_dashboard.origin`` (``web`` | ``desktop``). A desktop
dashboard is the backing dashboard Cinna Desktop creates for a desktop
dashboard's cloud tiles: it is left out of the web list by default and has its
own per-user limit. Existing rows are web dashboards.

Revision ID: 7c1d4e8a2b90
Revises: 056d6b9d9f87
Create Date: 2026-11-04 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '7c1d4e8a2b90'
down_revision = '056d6b9d9f87'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user_dashboard',
        sa.Column(
            'origin',
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
            server_default='web',
        ),
    )


def downgrade():
    op.drop_column('user_dashboard', 'origin')
