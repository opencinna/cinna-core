"""git_repo last_pushed_by_user_id (team library: who sent the latest version)

Revision ID: 08bfa6ee8d9f
Revises: 0e62eb9df5d4
Create Date: 2026-10-09 11:24:23.139028

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '08bfa6ee8d9f'
down_revision = '0e62eb9df5d4'
branch_labels = None
depends_on = None

FK_NAME = 'git_repo_last_pushed_by_user_id_fkey'


def upgrade():
    op.add_column('git_repo', sa.Column('last_pushed_by_user_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        FK_NAME, 'git_repo', 'user', ['last_pushed_by_user_id'], ['id'], ondelete='SET NULL'
    )


def downgrade():
    op.drop_constraint(FK_NAME, 'git_repo', type_='foreignkey')
    op.drop_column('git_repo', 'last_pushed_by_user_id')
