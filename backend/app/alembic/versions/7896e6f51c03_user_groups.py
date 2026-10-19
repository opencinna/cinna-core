"""user groups: groups, members, and every family's group-share table/column

One migration for the whole User Groups feature (docs/plans/user_groups_plan.md §1).

- ``user_group`` (case-insensitive unique name) and ``user_group_member``.
- Catalog: nullable ``group_id`` on ``bundle_access_grant`` and
  ``skill_package_access_grant`` with ``ck_*_grant_group_fk`` tying it to
  ``principal_kind = 'group'``. The existing ``ck_*_grant_user_fk`` stays valid
  (group rows have ``user_id`` NULL).
- ``credential_group_share``, ``ai_credential_group_share``,
  ``artifact_group_grant``, ``ai_knowledge_git_repo_group_share``.

Every SET NULL FK is DEFERRABLE INITIALLY DEFERRED (written explicitly, since
autogenerate drops it): deleting a user who is both a group's creator and its
member reaches the group through two cascade paths in one statement.

Revision ID: 7896e6f51c03
Revises: 98e0bd120428
Create Date: 2026-10-19 08:52:36.622165

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes

# revision identifiers, used by Alembic.
revision = '7896e6f51c03'
down_revision = '98e0bd120428'
branch_labels = None
depends_on = None


_CATALOG_GRANT_TABLES = (
    # (table, short prefix used by the existing constraint/index names)
    ('bundle_access_grant', 'bundle'),
    ('skill_package_access_grant', 'skill'),
)


def upgrade():
    op.create_table(
        'user_group',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['created_by_user_id'], ['user.id'],
            ondelete='SET NULL', deferrable=True, initially='DEFERRED',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_user_group_name_lower', 'user_group',
        [sa.literal_column('lower(name)')], unique=True,
    )

    op.create_table(
        'user_group_member',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['user_group.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'user_id', name='uq_user_group_member'),
    )
    op.create_index('ix_user_group_member_user', 'user_group_member', ['user_id'], unique=False)

    for table, prefix in _CATALOG_GRANT_TABLES:
        op.add_column(table, sa.Column('group_id', sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f'fk_{prefix}_grant_group', table, 'user_group',
            ['group_id'], ['id'], ondelete='CASCADE',
        )
        op.create_index(f'ix_{prefix}_grant_group', table, ['group_id'], unique=False)
        op.create_check_constraint(
            f'ck_{prefix}_grant_group_fk', table,
            "(principal_kind = 'group') = (group_id IS NOT NULL)",
        )

    op.create_table(
        'credential_group_share',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('credential_id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=False),
        sa.Column('shared_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('shared_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['credential_id'], ['credential.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_group.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['shared_by_user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('credential_id', 'group_id', name='uq_credential_group_share'),
    )
    op.create_index('ix_credential_group_share_group', 'credential_group_share', ['group_id'], unique=False)

    op.create_table(
        'ai_credential_group_share',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('ai_credential_id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=False),
        sa.Column('shared_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('shared_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['ai_credential_id'], ['ai_credential.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_group.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['shared_by_user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ai_credential_id', 'group_id', name='uq_ai_credential_group_share'),
    )
    op.create_index('ix_ai_credential_group_share_group', 'ai_credential_group_share', ['group_id'], unique=False)

    op.create_table(
        'artifact_group_grant',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('artifact_id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=False),
        sa.Column('level', sqlmodel.sql.sqltypes.AutoString(length=16), server_default='view', nullable=False),
        sa.Column('granted_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['artifact_id'], ['artifact.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_group.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['granted_by'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('artifact_id', 'group_id', name='uq_artifact_group_grant'),
    )
    op.create_index('ix_artifact_group_grant_group', 'artifact_group_grant', ['group_id'], unique=False)

    op.create_table(
        'ai_knowledge_git_repo_group_share',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('git_repo_id', sa.Uuid(), nullable=False),
        sa.Column('group_id', sa.Uuid(), nullable=False),
        sa.Column('granted_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['git_repo_id'], ['ai_knowledge_git_repo.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_group.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['granted_by_user_id'], ['user.id'],
            ondelete='SET NULL', deferrable=True, initially='DEFERRED',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('git_repo_id', 'group_id', name='uq_knowledge_repo_group_share'),
    )
    op.create_index(
        op.f('ix_ai_knowledge_git_repo_group_share_git_repo_id'),
        'ai_knowledge_git_repo_group_share', ['git_repo_id'], unique=False,
    )
    op.create_index(
        op.f('ix_ai_knowledge_git_repo_group_share_group_id'),
        'ai_knowledge_git_repo_group_share', ['group_id'], unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_ai_knowledge_git_repo_group_share_group_id'),
        table_name='ai_knowledge_git_repo_group_share',
    )
    op.drop_index(
        op.f('ix_ai_knowledge_git_repo_group_share_git_repo_id'),
        table_name='ai_knowledge_git_repo_group_share',
    )
    op.drop_table('ai_knowledge_git_repo_group_share')

    op.drop_index('ix_artifact_group_grant_group', table_name='artifact_group_grant')
    op.drop_table('artifact_group_grant')

    op.drop_index('ix_ai_credential_group_share_group', table_name='ai_credential_group_share')
    op.drop_table('ai_credential_group_share')

    op.drop_index('ix_credential_group_share_group', table_name='credential_group_share')
    op.drop_table('credential_group_share')

    for table, prefix in reversed(_CATALOG_GRANT_TABLES):
        # Group grants cannot survive without the column the check ties them to.
        op.execute(sa.text(f"DELETE FROM {table} WHERE principal_kind = 'group'"))
        op.drop_constraint(f'ck_{prefix}_grant_group_fk', table, type_='check')
        op.drop_index(f'ix_{prefix}_grant_group', table_name=table)
        op.drop_constraint(f'fk_{prefix}_grant_group', table, type_='foreignkey')
        op.drop_column(table, 'group_id')

    op.drop_index('ix_user_group_member_user', table_name='user_group_member')
    op.drop_table('user_group_member')

    op.drop_index('uq_user_group_name_lower', table_name='user_group')
    op.drop_table('user_group')
