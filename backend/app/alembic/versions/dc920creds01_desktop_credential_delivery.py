"""Desktop credential consent and revision tracking."""

from alembic import op
import sqlalchemy as sa

revision = "dc920creds01"
down_revision = "ba517c930def"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "credential",
        sa.Column(
            "allow_local_use", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "credential",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Database triggers cover OAuth refresh and every writer, including bulk updates.
    op.execute("""CREATE FUNCTION credential_touch_revision() RETURNS trigger AS $$
    BEGIN NEW.updated_at = clock_timestamp(); RETURN NEW; END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER credential_revision BEFORE UPDATE ON credential FOR EACH ROW EXECUTE FUNCTION credential_touch_revision()"
    )
    op.execute("""CREATE FUNCTION credential_share_touch_revision() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN UPDATE credential SET updated_at = clock_timestamp() WHERE id = OLD.credential_id; RETURN OLD;
      ELSE UPDATE credential SET updated_at = clock_timestamp() WHERE id = NEW.credential_id; RETURN NEW; END IF;
    END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER credential_share_revision AFTER INSERT OR UPDATE OR DELETE ON credential_shares FOR EACH ROW EXECUTE FUNCTION credential_share_touch_revision()"
    )


def downgrade():
    op.execute("DROP TRIGGER credential_share_revision ON credential_shares")
    op.execute("DROP FUNCTION credential_share_touch_revision()")
    op.execute("DROP TRIGGER credential_revision ON credential")
    op.execute("DROP FUNCTION credential_touch_revision()")
    op.drop_column("credential", "updated_at")
    op.drop_column("credential", "allow_local_use")
