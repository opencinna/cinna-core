"""add brand theme tables

One server-wide brand theme (drafts + at most one published row) and its
content-addressed assets. Publish revisions come from a sequence so a
revision is never reused, even after every row is deleted: a desktop holding
an old revision as its ETag must never get a 304 for a different theme.

Revision ID: f725f8313224
Revises: 3e9a6c2d8f41
Create Date: 2026-11-06 20:34:48.543706

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'f725f8313224'
down_revision = '3e9a6c2d8f41'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('brand_theme_asset',
    sa.Column('sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('mime', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
    sa.Column('bytes', sa.Integer(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('sha256')
    )
    op.create_table('brand_theme',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('theme_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('asset_map', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('revision', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('published_by_id', sa.Uuid(), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['published_by_id'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_brand_theme_one_published', 'brand_theme', ['status'], unique=True, postgresql_where=sa.text("status = 'published'"))
    op.execute("CREATE SEQUENCE brand_theme_revision_seq START WITH 1")


def downgrade():
    op.execute("DROP SEQUENCE IF EXISTS brand_theme_revision_seq")
    op.drop_index('uq_brand_theme_one_published', table_name='brand_theme', postgresql_where=sa.text("status = 'published'"))
    op.drop_table('brand_theme')
    op.drop_table('brand_theme_asset')
