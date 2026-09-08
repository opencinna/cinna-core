"""add claim_held_default_slots to managed ai credential membership

Whether the grant that created a membership row may take an SDK default slot its
owner already occupies.

The column exists because the answer is needed at a different time from when it
is known. A shared member's defaults are wired inside the grant, with the
caller's intent still on the stack; a **minted** member's are wired later, by a
converge pass that cannot tell whether a superuser pressed "apply to existing
users" or an account simply signed up. Without somewhere to record it, that pass
has to guess, and either guess is wrong for half its callers — automatic
provisioning would steal defaults it must never take, or the two deliberate
escape hatches would stop overwriting for exactly the provider kind (per-user
minted keys) the feature's headline scenario uses.

``false`` for every existing row, which is the safe direction and the correct
one: nothing that has already been granted should retroactively acquire
permission to displace a default somebody chose.

Revision ID: 45938a69aee7
Revises: c23d6b59a8f5
Create Date: 2026-09-07 16:57:35.409349
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '45938a69aee7'
down_revision = 'c23d6b59a8f5'
branch_labels = None
depends_on = None


def upgrade():
    # Autogenerate also proposed three ``cli_device_login_request`` timezone
    # alterations. They are pre-existing model/schema drift in an unrelated
    # feature, not part of this change, and are deliberately not carried here:
    # a migration that quietly retypes another feature's timestamp columns is a
    # migration nobody can review or revert in isolation.
    op.add_column(
        'managed_ai_credential_membership',
        sa.Column(
            'claim_held_default_slots',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_column(
        'managed_ai_credential_membership', 'claim_held_default_slots'
    )
