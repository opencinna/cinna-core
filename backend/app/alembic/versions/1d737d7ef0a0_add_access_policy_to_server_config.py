"""add access policy to server config

Moves the front-door policy out of env settings and into the ``server_config``
singleton: registration mode, the allowed-email pattern list, the password-auth
and Google auto-register switches, the default role for new accounts, and the
invitation wizard's desktop default.

The data step converts an existing install's env configuration once, so an
instance that was gating signups through ``AUTH_WHITELIST_USER_DOMAINS`` keeps
gating them after the upgrade. Domains become globs: ``acme.com`` →
``*@acme.com``. A fresh instance with no row yet gets the same seed from
``ServerConfigService.get_or_create`` when the row is first created.

Revision ID: 1d737d7ef0a0
Revises: 68aab27946e5
Create Date: 2026-09-05 19:06:53.780571

"""
import logging

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision = '1d737d7ef0a0'
down_revision = '68aab27946e5'
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade():
    # Server defaults are kept (not dropped afterwards): they are the stable
    # values the model declares, and keeping them means a row inserted by a
    # tool that does not know these columns still lands in a legal state.
    op.add_column(
        'server_config',
        sa.Column(
            'registration_mode',
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
            server_default='open',
        ),
    )
    op.add_column(
        'server_config',
        sa.Column(
            'allowed_email_patterns',
            sa.Text(),
            nullable=False,
            server_default='',
        ),
    )
    op.add_column(
        'server_config',
        sa.Column(
            'password_auth_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        'server_config',
        sa.Column(
            'google_auto_register',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.add_column(
        'server_config',
        sa.Column(
            'default_user_role',
            sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
            server_default='agent-user',
        ),
    )
    op.add_column(
        'server_config',
        sa.Column(
            'invite_include_desktop_default',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )

    _seed_from_env()


def _seed_from_env():
    """Carry the retired env settings onto an existing config row, once.

    Wrapped in a broad guard on purpose: an install whose ``.env`` cannot be
    parsed here (alembic run with a reduced environment) must still get its
    schema — a failed migration stops the deploy.

    But note the direction of that failure. If the seed is skipped, the row
    keeps ``allowed_email_patterns=''``, which the policy reads as **no
    restriction**: an install that was gating signups by domain comes up open
    to every domain. So the skip is logged at error, and
    ``AccessPolicyService.warn_if_env_overrides_present`` warns again at every
    subsequent startup while the env setting is still present. An operator
    seeing either must check the Access tab.
    """
    try:
        from app.core.config import settings

        domains = [
            d.strip().lower()
            for d in (settings.AUTH_WHITELIST_USER_DOMAINS or "").split(",")
            if d.strip()
        ]
        patterns = ", ".join(f"*@{d}" for d in domains)
        role = settings.DEFAULT_USER_ROLE
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Access-policy env seed SKIPPED (settings unavailable: %s). If "
            "AUTH_WHITELIST_USER_DOMAINS was set, signup is now UNRESTRICTED "
            "until you set the patterns at /admin/server-configuration#access.",
            exc,
        )
        return

    if not patterns and role == "agent-user":
        # Nothing to carry over — the column defaults already say this.
        return

    # UPDATE with no WHERE on a singleton table: it touches the one row if it
    # exists and zero rows if it does not, which is exactly the intent. A
    # fresh instance has no row yet and is seeded at first access instead.
    op.execute(
        sa.text(
            "UPDATE server_config SET allowed_email_patterns = :patterns, "
            "default_user_role = :role"
        ).bindparams(patterns=patterns, role=role)
    )


def downgrade():
    op.drop_column('server_config', 'invite_include_desktop_default')
    op.drop_column('server_config', 'default_user_role')
    op.drop_column('server_config', 'google_auto_register')
    op.drop_column('server_config', 'password_auth_enabled')
    op.drop_column('server_config', 'allowed_email_patterns')
    op.drop_column('server_config', 'registration_mode')
