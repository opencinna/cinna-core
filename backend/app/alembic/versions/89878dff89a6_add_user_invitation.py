"""add user invitation

Zero-touch onboarding, phase 3. One new table, ``user_invitation``: one row per
invited account, reused for the account's lifetime, with ``token_jti`` rotated
in place on every resend.

Two foreign keys, two different ``ondelete`` clauses, and both are behavioural
rather than cosmetic:

* ``user_id ON DELETE CASCADE`` — ``users.delete_user`` is a bare
  ``session.delete(user)`` with no ORM cascade behind it, and the model
  deliberately declares no ``Relationship``. Postgres is the entire deletion
  mechanism; without CASCADE here, deleting an invited account raises a foreign
  key violation.
* ``invited_by_id ON DELETE SET NULL`` — removing the administrator who issued
  an invitation must not remove the invitation. Getting this backwards would
  make offboarding an admin silently invalidate every outstanding invite, and
  nothing in the application would report it.

Indexes: the two unique ones and nothing else. Ruling Q6 dropped
``ix_user_invitation_expires_at`` — see the comment in ``upgrade``.

No server defaults are emitted for ``auth_hint`` / ``include_desktop`` /
``send_count``, deliberately: the model carries Python-side defaults, every
insert into this table goes through the ORM, and a ``server_default`` the model
does not declare is drift that the next ``--autogenerate`` proposes removing.

Autogenerate also proposed three ``cli_device_login_request`` timestamp
alterations, exactly as it did for ``b71863b32aa1``. They are pre-existing drift
between that model and the column types actually in the database (the model is
the wrong side, not the DB), they are unrelated to invitations, and applying
them would drop the timezone from live rows. Removed here for the same reason
they were removed there.

Revision ID: 89878dff89a6
Revises: b71863b32aa1
Create Date: 2026-09-06 01:11:29.449407

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '89878dff89a6'
down_revision = 'b71863b32aa1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_invitation',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('token_jti', sa.Uuid(), nullable=False),
        sa.Column('invited_by_id', sa.Uuid(), nullable=True),
        sa.Column(
            'auth_hint',
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
        ),
        sa.Column('include_desktop', sa.Boolean(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('send_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['invited_by_id'], ['user.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # The only two read paths there are: an admin projection resolves an
    # invitation by ``user_id``, and token verification resolves it by
    # ``token_jti``. Unique in both cases — one invitation per account, and one
    # live jti per invitation, which is what makes rotation revoke.
    op.create_index(
        'uq_user_invitation_token_jti',
        'user_invitation',
        ['token_jti'],
        unique=True,
    )
    op.create_index(
        'uq_user_invitation_user_id',
        'user_invitation',
        ['user_id'],
        unique=True,
    )
    # NOTE — no index on ``expires_at``, and that is a decision, not an
    # oversight (ruling Q6). Nothing in phase 3 queries by expiry: it is
    # evaluated in Python on a row already fetched by one of the two keys
    # above. The expiry sweeper that would scan by it does not exist yet.
    # Whoever adds that sweeper should add ``ix_user_invitation_expires_at``
    # in the same migration, so the index arrives with its first consumer
    # instead of years ahead of it. The model declares no ``index=True`` on
    # the column, so ``--autogenerate`` will not re-propose it in the
    # meantime.


def downgrade():
    op.drop_index('uq_user_invitation_user_id', table_name='user_invitation')
    op.drop_index('uq_user_invitation_token_jti', table_name='user_invitation')
    op.drop_table('user_invitation')
    # ``UserPublic.invitation_status`` needs no downgrade: it is a projection
    # field computed by the route layer, never a column.
