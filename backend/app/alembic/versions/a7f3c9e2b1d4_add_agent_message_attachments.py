"""add_agent_message_attachments

Revision ID: a7f3c9e2b1d4
Revises: cdfb21cadb62
Create Date: 2026-06-06 00:00:00.000000

Adds columns supporting agent-authored message attachments:
- file_uploads.origin (VARCHAR NOT NULL default 'user') — 'user' | 'agent'
- file_uploads.session_id (UUID NULL, FK -> session.id ON DELETE SET NULL)
- index ix_file_uploads_session_id
- message_files.source (VARCHAR NOT NULL default 'user_upload')
  — 'user_upload' | 'agent_attachment'
- message_files.event_seq (INTEGER NULL) — inline ordering position

Defaults make all existing rows valid; no data backfill required.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = 'a7f3c9e2b1d4'
down_revision = 'cdfb21cadb62'
branch_labels = None
depends_on = None


def upgrade():
    # file_uploads
    op.add_column(
        'file_uploads',
        sa.Column(
            'origin',
            sqlmodel.sql.sqltypes.AutoString(length=31),
            nullable=False,
            server_default='user',
        ),
    )
    op.add_column(
        'file_uploads',
        sa.Column('session_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_file_uploads_session_id_session',
        'file_uploads',
        'session',
        ['session_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_file_uploads_session_id',
        'file_uploads',
        ['session_id'],
        unique=False,
    )

    # message_files
    op.add_column(
        'message_files',
        sa.Column(
            'source',
            sqlmodel.sql.sqltypes.AutoString(length=31),
            nullable=False,
            server_default='user_upload',
        ),
    )
    op.add_column(
        'message_files',
        sa.Column('event_seq', sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column('message_files', 'event_seq')
    op.drop_column('message_files', 'source')

    op.drop_index('ix_file_uploads_session_id', table_name='file_uploads')
    op.drop_constraint(
        'fk_file_uploads_session_id_session',
        'file_uploads',
        type_='foreignkey',
    )
    op.drop_column('file_uploads', 'session_id')
    op.drop_column('file_uploads', 'origin')
