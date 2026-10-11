"""agent auto_generate_description

Opt-in for the model-written agent description (bug 5). New agents default to
off; existing agents keep today's behaviour (on), except foreign installs
(consumer installs of a published bundle): their description is the
publisher's, which a regeneration would overwrite.

Revision ID: c4e1a9d27b63
Revises: bfd8b2ff2e3c
Create Date: 2026-10-11 10:40:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c4e1a9d27b63'
down_revision = 'bfd8b2ff2e3c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'agent',
        sa.Column(
            'auto_generate_description',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        "UPDATE agent SET auto_generate_description = true "
        "WHERE NOT (bundle_uuid IS NOT NULL AND is_publisher_install = false)"
    )


def downgrade():
    op.drop_column('agent', 'auto_generate_description')
