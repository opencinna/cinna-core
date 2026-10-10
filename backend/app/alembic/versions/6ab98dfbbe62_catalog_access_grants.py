"""catalog_access_grants: one access model for bundles and skill packages

Both grant tables (``bundle_access_grant``, ``skill_package_access_grant``)
gain ``principal_kind`` (``user``/``role``), ``principal_id`` (user uuid as
text, or the role value) and ``access`` (``use``/``edit``/``maintain``).
``user_id`` becomes nullable and is set only for user grants (check
``ck_*_grant_user_fk``); the unique key moves from ``(item, user_id)`` to
``(item, principal_kind, principal_id)``.

Every existing row becomes ``user/use`` (its old meaning). Each item's
publisher becomes a ``maintain`` grant: an existing row of theirs is raised,
otherwise a row is inserted with ``granted_by_user_id`` = the publisher.

Downgrade is LOSSY: role grants are deleted; publisher ``maintain`` rows the
upgrade (or a later first publish) created — ``access='maintain'`` and
``granted_by_user_id = user_id = publisher`` — are deleted, because the old
model gives the publisher access implicitly; every other ``edit``/``maintain``
user row falls back to a plain grant (the level is dropped).

Revision ID: 6ab98dfbbe62
Revises: 08bfa6ee8d9f
Create Date: 2026-10-10 08:15:42.985892

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '6ab98dfbbe62'
down_revision = '08bfa6ee8d9f'
branch_labels = None
depends_on = None


# (grant table, item fk column, item table, old unique, new unique, check)
_TABLES = (
    (
        "bundle_access_grant",
        "bundle_id",
        "agent_bundle",
        "uq_bundle_grant_bundle_user",
        "uq_bundle_grant_principal",
        "ck_bundle_grant_user_fk",
    ),
    (
        "skill_package_access_grant",
        "package_id",
        "skill_package",
        "uq_skill_grant_package_user",
        "uq_skill_grant_principal",
        "ck_skill_grant_user_fk",
    ),
)


def upgrade():
    for table, fk, item_table, old_uq, new_uq, check in _TABLES:
        # 1. Columns, backfilled: every existing row is a user/use grant.
        op.add_column(table, sa.Column(
            'principal_kind', sqlmodel.sql.sqltypes.AutoString(length=16),
            server_default='user', nullable=False,
        ))
        op.add_column(table, sa.Column(
            'principal_id', sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=True,
        ))
        op.add_column(table, sa.Column(
            'access', sqlmodel.sql.sqltypes.AutoString(length=16),
            server_default='use', nullable=False,
        ))
        op.execute(f"UPDATE {table} SET principal_id = user_id::text")
        op.alter_column(table, 'principal_id', nullable=False)

        # 2. user_id nullable; unique key and check on the principal.
        op.alter_column(table, 'user_id', existing_type=sa.UUID(), nullable=True)
        op.drop_constraint(old_uq, table, type_='unique')
        op.create_unique_constraint(new_uq, table, [fk, 'principal_kind', 'principal_id'])
        op.create_check_constraint(
            check, table, "(principal_kind = 'user') = (user_id IS NOT NULL)"
        )

        # 3. The publisher becomes a maintainer.
        op.execute(f"""
            UPDATE {table} AS g SET access = 'maintain'
            FROM {item_table} AS i
            WHERE g.{fk} = i.id
              AND g.principal_kind = 'user'
              AND g.user_id = i.publisher_user_id
        """)
        op.execute(f"""
            INSERT INTO {table}
                (id, {fk}, principal_kind, principal_id, user_id, access,
                 granted_by_user_id, created_at)
            SELECT gen_random_uuid(), i.id, 'user', i.publisher_user_id::text,
                   i.publisher_user_id, 'maintain', i.publisher_user_id, now()
            FROM {item_table} AS i
            WHERE i.publisher_user_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {table} AS g
                  WHERE g.{fk} = i.id
                    AND g.principal_kind = 'user'
                    AND g.user_id = i.publisher_user_id
              )
        """)


def downgrade():
    for table, fk, item_table, old_uq, new_uq, check in _TABLES:
        # 1. Role grants have no old shape.
        op.execute(f"DELETE FROM {table} WHERE principal_kind <> 'user'")
        # 2. Publisher maintain rows the new model created.
        op.execute(f"""
            DELETE FROM {table} AS g
            USING {item_table} AS i
            WHERE g.{fk} = i.id
              AND g.user_id = i.publisher_user_id
              AND g.access = 'maintain'
              AND g.granted_by_user_id = g.user_id
        """)
        # 3. Remaining edit/maintain rows fall back to plain grants (lossy:
        #    the access column is dropped below).
        # 4. Old constraints and columns.
        op.drop_constraint(check, table, type_='check')
        op.drop_constraint(new_uq, table, type_='unique')
        op.create_unique_constraint(old_uq, table, [fk, 'user_id'])
        op.alter_column(table, 'user_id', existing_type=sa.UUID(), nullable=False)
        op.drop_column(table, 'access')
        op.drop_column(table, 'principal_id')
        op.drop_column(table, 'principal_kind')
