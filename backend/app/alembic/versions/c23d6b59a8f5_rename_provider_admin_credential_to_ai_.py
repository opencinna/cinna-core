"""rename provider_admin_credential to ai_provider and split the provisioning rule off managed_ai_credential

Revision ID: c23d6b59a8f5
Revises: ed8d6a23f13c
Create Date: 2026-09-07 14:55:54.211698

One record used to be both a credential and its own factory:
``managed_ai_credential`` held a key *and* the rule that handed that key out.
This splits the two.

- ``provider_admin_credential`` is **renamed** to ``ai_provider`` — not
  replaced — so every existing minted setup keeps working with no data movement,
  and gains the rule (``auto_provision_roles``) plus the whole wiring policy.
- ``managed_ai_credential.provider_admin_credential_id`` becomes ``provider_id``
  with **ON DELETE RESTRICT**, and loses ``auto_provision_roles`` and
  ``provisioning_mode``. Its policy columns stay and remain the real values for
  a *manual* record; on a provider-owned record they are shadowed.

WHAT THE SECRET COLUMN MEANS AFTER THIS
---------------------------------------
``ai_provider.encrypted_secret`` now holds two categorically different things,
keyed to ``kind``: an organisation administration secret for ``minted`` (what
the column always held), and an ordinary model API key for ``fixed_key``.

**The ``fixed_key`` rows this migration creates carry the credential's key
verbatim**, which means they are in ``ai_credential.encrypted_data`` codec —
Fernet over ``{"api_key": ..., "base_url"?: ..., "model"?: ...}`` — not a bare
key string. Re-encrypting here would mean decrypting every company key inside a
schema migration, so the bytes are moved unchanged instead. Whatever writes a
``fixed_key`` secret later has to use that same envelope —
``tests/unit/test_managed_credential_key_source.py`` is what holds the reading
side to it.

WHY THE BACKFILL REFUSES INSTEAD OF SKIPPING
--------------------------------------------
A row this migration cannot classify is a row whose key stops being handed out.
Nobody notices that on the day it happens; an admin notices weeks later when a
new hire has no key. So each shape ``_reject_unclassifiable`` knows about raises
with the offending id in the message, and does so **before the backfill moves a
single row** — an abort that has already half-moved a company key is worse than
one that has not. Every check below has a test in
``tests/migrations/ai_provider_split_test.py`` that seeds the shape and asserts
the upgrade aborts naming it; so does the round trip over the five shapes §12 of
the plan lists, and so does the downgrade refusal.

WHAT THE DOWNGRADE CANNOT UNDO, STATED RATHER THAN GUESSED
-----------------------------------------------------------
- Split copies (step 4a's ``>1`` branch) are **not** merged back. There is no
  record of which row was the original, and merging would delete an
  administration secret that is the only means of revoking keys minted through
  it.
- A ``fixed_key`` provider's ``last_verified_at``, ``last_verify_error`` and
  ``config`` are dropped when it folds back into its credential. The old schema
  has nowhere to put them; the key, the rule and the policy all survive.
- A ``fixed_key`` provider with an empty ``auto_provision_roles`` cannot have
  been created by this migration — step 4b only ever built one from a credential
  that *had* roles — so it postdates the upgrade. If its credential has members,
  the downgrade refuses rather than folding a configuration it has no record of
  into a manual credential behind the admin's back.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'c23d6b59a8f5'
down_revision = 'ed8d6a23f13c'
branch_labels = None
depends_on = None


# The FK ``ed8d6a23f13c`` created, named there so it can be dropped by name.
OLD_PROVIDER_FK = "fk_managed_ai_credential_provider_admin_credential"
# Its replacement, pointing at the renamed table with RESTRICT.
NEW_PROVIDER_FK = "fk_managed_ai_credential_provider"

# Every policy column that moves from the credential up onto the provider. The
# credential keeps its copy (shadowed on provider-owned rows, live on manual
# ones), so this is a copy rather than a move — except ``auto_provision_roles``
# and ``provisioning_mode``, which are dropped in step 5.
POLICY_COLUMNS = (
    "set_as_default",
    "set_user_sdk_defaults",
    "sdk_default_modes",
    "default_model",
    "available_models",
    "model_override_conversation",
    "model_override_building",
)


def _reject_unclassifiable() -> None:
    """Refuse the shapes the backfill has no correct answer for.

    One test per check in ``tests/migrations/ai_provider_split_test.py``. The
    list is not a proof that nothing else can go wrong — it is the set of
    wrong shapes we know how to recognise, and the post-condition in
    :func:`_assert_one_credential_per_provider` is the backstop under it.

    Runs after the renames of steps 1-3 (hence the new column names) but
    before the backfill changes a single row. Each check names the offending
    ids, because a
    migration that says "some row is wrong" sends an administrator hunting
    through a table they cannot query in the same shape the migration saw.
    """
    conn = op.get_bind()

    def ids(sql: str) -> list[str]:
        return [str(row[0]) for row in conn.execute(sa.text(sql)).fetchall()]

    # 1. ``auto_provision_roles`` that is not a JSON array. Everything below
    #    calls ``json_array_length`` on it, which would raise a bare Postgres
    #    type error with no id in it.
    bad_json = ids(
        "SELECT id FROM managed_ai_credential "
        "WHERE json_typeof(auto_provision_roles) <> 'array'"
    )
    if bad_json:
        raise RuntimeError(
            "managed_ai_credential.auto_provision_roles is not a JSON array on: "
            f"{', '.join(bad_json)}. Fix these rows before migrating."
        )

    # 2. A shared credential that hands itself out but holds no key. There is
    #    nothing to build a fixed_key provider from, and dropping the rule
    #    silently is how a company key stops reaching new hires.
    roles_no_key = ids(
        "SELECT id FROM managed_ai_credential "
        "WHERE provisioning_mode = 'shared' "
        "AND json_array_length(auto_provision_roles) > 0 "
        "AND encrypted_data IS NULL"
    )
    if roles_no_key:
        raise RuntimeError(
            "Cannot classify managed AI credential(s) "
            f"{', '.join(roles_no_key)}: they auto-provision by role but hold "
            "no key, so there is no key source to turn into a provider."
        )

    # 3. A minted credential with nothing to mint through.
    minted_no_provider = ids(
        "SELECT id FROM managed_ai_credential "
        "WHERE provisioning_mode = 'minted' "
        "AND provider_id IS NULL"
    )
    if minted_no_provider:
        raise RuntimeError(
            "Cannot classify managed AI credential(s) "
            f"{', '.join(minted_no_provider)}: they are in minted mode with no "
            "provider admin credential to mint through."
        )

    # 4. A shared credential pointing at an administration secret. The two
    #    kinds are not interchangeable — the pointer says "mint", the mode says
    #    "copy one key" — and picking either reading rewrites what its members
    #    hold.
    shared_with_provider = ids(
        "SELECT id FROM managed_ai_credential "
        "WHERE provisioning_mode = 'shared' "
        "AND provider_id IS NOT NULL"
    )
    if shared_with_provider:
        raise RuntimeError(
            "Cannot classify managed AI credential(s) "
            f"{', '.join(shared_with_provider)}: they are in shared mode but "
            "point at a provider admin credential."
        )

    # 5. A type nothing can talk to. Asked of the registry rather than a list
    #    written down here, so this check cannot drift from the adapters.
    from app.services.ai_providers import registry

    unknown_provider = [
        f"ai_provider {row[0]} (type {row[1]!r})"
        for row in conn.execute(
            sa.text("SELECT id, provider_type FROM ai_provider")
        ).fetchall()
        if registry.find_adapter(row[1]) is None
    ]
    unknown_credential = [
        f"managed_ai_credential {row[0]} (type {row[1]!r})"
        for row in conn.execute(
            sa.text(
                "SELECT id, type FROM managed_ai_credential "
                "WHERE json_array_length(auto_provision_roles) > 0"
            )
        ).fetchall()
        if registry.find_adapter(row[1]) is None
    ]
    unknown = unknown_provider + unknown_credential
    if unknown:
        raise RuntimeError(
            "No AI provider adapter serves the type of: "
            f"{'; '.join(unknown)}. These cannot become providers."
        )


def _backfill() -> None:
    """Steps 4a, 4b and 4c, in that order — later steps rely on earlier ones."""
    conn = op.get_bind()

    # ---- 4a: one provider, one managed credential ------------------------
    # Every ai_provider row at this point came from provider_admin_credential
    # and is therefore ``minted``.
    for (provider_id,) in conn.execute(
        sa.text("SELECT id FROM ai_provider")
    ).fetchall():
        children = conn.execute(
            sa.text(
                "SELECT id, name FROM managed_ai_credential "
                "WHERE provider_id = :pid ORDER BY created_at, id"
            ),
            {"pid": provider_id},
        ).fetchall()

        if not children:
            # Nothing mints through it yet. Give it the empty credential the
            # 1:1 invariant requires, rather than leaving a provider that no
            # member list hangs off.
            conn.execute(
                sa.text(
                    """
                    INSERT INTO managed_ai_credential (
                        id, name, type, encrypted_data, provider_id,
                        provisioning_mode, auto_provision_roles,
                        sdk_default_modes, set_as_default,
                        set_user_sdk_defaults, managed_by_id,
                        created_at, updated_at
                    )
                    SELECT
                        gen_random_uuid(), p.name, p.provider_type, NULL, p.id,
                        'minted', '[]'::json,
                        '["conversation", "building"]'::json, false, false,
                        p.created_by_id, (NOW() AT TIME ZONE 'UTC'), (NOW() AT TIME ZONE 'UTC')
                    FROM ai_provider p WHERE p.id = :pid
                    """
                ),
                {"pid": provider_id},
            )
            continue

        # The split. The first credential keeps the original provider; each
        # additional one gets a copy of it, secret included. Duplicating an
        # administration secret is the accepted consequence of storing it on
        # the provider — the alternative is a provider shared by two member
        # lists, which is the shape this whole change removes.
        for child_id, child_name in children[1:]:
            new_id = conn.execute(
                sa.text(
                    """
                    INSERT INTO ai_provider (
                        id, name, kind, provider_type, encrypted_secret, config,
                        auto_provision_roles, sdk_default_modes, set_as_default,
                        set_user_sdk_defaults, last_verified_at,
                        last_verify_error, created_by_id, created_at, updated_at
                    )
                    SELECT
                        gen_random_uuid(),
                        LEFT(p.name || ' — ' || :child_name, 255),
                        'minted', p.provider_type, p.encrypted_secret, p.config,
                        '[]'::json, '["conversation", "building"]'::json,
                        false, false,
                        p.last_verified_at, p.last_verify_error,
                        p.created_by_id, (NOW() AT TIME ZONE 'UTC'), (NOW() AT TIME ZONE 'UTC')
                    FROM ai_provider p WHERE p.id = :pid
                    RETURNING id
                    """
                ),
                {"pid": provider_id, "child_name": child_name},
            ).scalar_one()
            conn.execute(
                sa.text(
                    "UPDATE managed_ai_credential SET provider_id = :new "
                    "WHERE id = :cid"
                ),
                {"new": new_id, "cid": child_id},
            )

    # ---- 4b: a shared credential with a rule becomes a fixed_key provider --
    rows = conn.execute(
        sa.text(
            "SELECT id FROM managed_ai_credential "
            "WHERE provider_id IS NULL "
            "AND json_array_length(auto_provision_roles) > 0"
        )
    ).fetchall()
    for (credential_id,) in rows:
        new_id = conn.execute(
            sa.text(
                """
                INSERT INTO ai_provider (
                    id, name, kind, provider_type, encrypted_secret, config,
                    base_url, model, auto_provision_roles, set_as_default,
                    set_user_sdk_defaults, sdk_default_modes, default_model,
                    available_models, model_override_conversation,
                    model_override_building, expiry_notification_date,
                    created_by_id, created_at, updated_at
                )
                SELECT
                    gen_random_uuid(), c.name, 'fixed_key', c.type,
                    c.encrypted_data, '{}'::json,
                    c.base_url, c.model, c.auto_provision_roles,
                    c.set_as_default, c.set_user_sdk_defaults,
                    c.sdk_default_modes, c.default_model, c.available_models,
                    c.model_override_conversation, c.model_override_building,
                    c.expiry_notification_date AT TIME ZONE 'UTC',
                    c.managed_by_id, c.created_at, (NOW() AT TIME ZONE 'UTC')
                FROM managed_ai_credential c WHERE c.id = :cid
                RETURNING id
                """
            ),
            {"cid": credential_id},
        ).scalar_one()
        # The provider is now the only source of truth for the key.
        conn.execute(
            sa.text(
                "UPDATE managed_ai_credential "
                "SET provider_id = :pid, encrypted_data = NULL "
                "WHERE id = :cid"
            ),
            {"pid": new_id, "cid": credential_id},
        )

    # ---- 4c: copy each remaining parent's policy up onto its provider -----
    # After 4b a fixed_key provider and its credential both carry base_url and
    # model. The **provider's** are the live ones from here on — the credential's
    # are shadowed with everything else in that block — which is why 4b copies
    # them up rather than moving them, and why nothing below clears the
    # credential's copy: the downgrade reads it back.
    # ``base_url``/``model`` are deliberately not copied here: they describe a
    # pasted key, and every provider this touches is ``minted``, whose members'
    # keys carry their own. ``auto_provision_roles`` *is* copied — it is about
    # to be dropped, and nothing else preserves it.
    assignments = ", ".join(f"{col} = c.{col}" for col in POLICY_COLUMNS)
    conn.execute(
        sa.text(
            f"""
            UPDATE ai_provider p
            SET {assignments},
                auto_provision_roles = c.auto_provision_roles,
                expiry_notification_date =
                    c.expiry_notification_date AT TIME ZONE 'UTC',
                updated_at = (NOW() AT TIME ZONE 'UTC')
            FROM managed_ai_credential c
            WHERE c.provider_id = p.id AND p.kind = 'minted'
            """
        )
    )


def _assert_one_credential_per_provider() -> None:
    """Step 6's post-condition: the 1:1 invariant the whole design rests on."""
    conn = op.get_bind()

    wrong = conn.execute(
        sa.text(
            """
            SELECT p.id, COUNT(c.id) AS n
            FROM ai_provider p
            LEFT JOIN managed_ai_credential c ON c.provider_id = p.id
            GROUP BY p.id
            HAVING COUNT(c.id) <> 1
            """
        )
    ).fetchall()
    if wrong:
        detail = ", ".join(f"provider={row[0]} credentials={row[1]}" for row in wrong)
        raise RuntimeError(
            "A provider must own exactly one managed AI credential after this "
            f"migration. It does not for: {detail}. A count of 0 means step 4a "
            "did not run for that provider; more than 1 means the split did not "
            "separate them. Neither is recoverable here — nothing has been "
            "committed, so re-run against a fixed database."
        )

    stranded = conn.execute(
        sa.text(
            "SELECT c.id FROM managed_ai_credential c "
            "JOIN ai_provider p ON p.id = c.provider_id "
            "WHERE c.encrypted_data IS NOT NULL"
        )
    ).fetchall()
    if stranded:
        detail = ", ".join(str(row[0]) for row in stranded)
        raise RuntimeError(
            "Provider-owned managed AI credential(s) still hold a key of their "
            f"own, so there are now two sources of truth for it: {detail}. NULL "
            "their encrypted_data if the provider's copy is the current one, or "
            "clear their provider_id if it is not, and re-run."
        )


def upgrade():
    # ---- 1. rename the table, its index and its constraints --------------
    op.rename_table("provider_admin_credential", "ai_provider")
    op.execute(
        "ALTER INDEX ix_provider_admin_credential_type "
        "RENAME TO ix_ai_provider_type"
    )
    op.execute(
        "ALTER TABLE ai_provider RENAME CONSTRAINT "
        "provider_admin_credential_pkey TO ai_provider_pkey"
    )
    op.execute(
        "ALTER TABLE ai_provider RENAME CONSTRAINT "
        "provider_admin_credential_created_by_id_fkey "
        "TO ai_provider_created_by_id_fkey"
    )

    # ---- 2. the rule and the wiring policy -------------------------------
    # ``kind`` defaults to 'minted' on the server because every pre-existing row
    # in this table is an organisation administration secret. The model has no
    # Python-side default, so a new provider must say which it is.
    op.add_column(
        "ai_provider",
        sa.Column("kind", sa.String(length=16), server_default="minted", nullable=False),
    )
    op.add_column("ai_provider", sa.Column("base_url", sa.String(length=500), nullable=True))
    op.add_column("ai_provider", sa.Column("model", sa.String(length=255), nullable=True))
    op.add_column(
        "ai_provider",
        sa.Column(
            "auto_provision_roles",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_provider",
        sa.Column("set_as_default", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "ai_provider",
        sa.Column(
            "set_user_sdk_defaults", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "ai_provider",
        sa.Column(
            "sdk_default_modes",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("""'["conversation", "building"]'::json"""),
            nullable=False,
        ),
    )
    op.add_column("ai_provider", sa.Column("default_model", sa.String(length=255), nullable=True))
    op.add_column(
        "ai_provider",
        sa.Column("available_models", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "ai_provider",
        sa.Column("model_override_conversation", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "ai_provider", sa.Column("model_override_building", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "ai_provider",
        sa.Column("expiry_notification_date", sa.DateTime(timezone=True), nullable=True),
    )
    # On ``kind``, not on the JSON roles column: the auto-provision scan reads
    # every provider row and filters roles in Python.
    op.create_index("ix_ai_provider_auto_provision", "ai_provider", ["kind"], unique=False)

    # ---- 3. the credential's pointer -------------------------------------
    # ``ed8d6a23f13c`` created no index on this column, so there is none to
    # rename — only the constraint.
    op.drop_constraint(OLD_PROVIDER_FK, "managed_ai_credential", type_="foreignkey")
    op.alter_column(
        "managed_ai_credential",
        "provider_admin_credential_id",
        new_column_name="provider_id",
    )
    op.create_foreign_key(
        NEW_PROVIDER_FK,
        "managed_ai_credential",
        "ai_provider",
        ["provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ---- 4. backfill, refusing anything it cannot classify ----------------
    _reject_unclassifiable()
    _backfill()

    # ---- 5. the rule and the mode are no longer the credential's ---------
    op.drop_column("managed_ai_credential", "auto_provision_roles")
    op.drop_column("managed_ai_credential", "provisioning_mode")

    # ---- 6. post-conditions ----------------------------------------------
    _assert_one_credential_per_provider()


def downgrade():
    conn = op.get_bind()

    # Restore the columns first: everything below writes into them.
    op.add_column(
        "managed_ai_credential",
        sa.Column("provisioning_mode", sa.String(length=24), server_default="shared", nullable=False),
    )
    op.add_column(
        "managed_ai_credential",
        sa.Column(
            "auto_provision_roles",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )

    # A fixed_key provider with no rule cannot have come from step 4b, which
    # only ever built one from a credential that had roles. It postdates the
    # upgrade, this migration has no record of what it was, and folding it into
    # a manual credential would leave its members holding a key from a
    # configuration that silently disappeared. Refuse and name it.
    unknown = conn.execute(
        sa.text(
            """
            SELECT p.id, p.name, COUNT(m.id) AS members
            FROM ai_provider p
            JOIN managed_ai_credential c ON c.provider_id = p.id
            JOIN managed_ai_credential_membership m
              ON m.managed_credential_id = c.id
            WHERE p.kind = 'fixed_key'
              AND json_array_length(p.auto_provision_roles) = 0
            GROUP BY p.id, p.name
            """
        )
    ).fetchall()
    if unknown:
        detail = ", ".join(f"{row[0]} ({row[1]!r}, {row[2]} member(s))" for row in unknown)
        raise RuntimeError(
            "Cannot downgrade: AI provider(s) created after this migration have "
            f"members holding their key: {detail}. Delete them through the admin "
            "API first, which removes their members' credentials in the right "
            "order."
        )

    # Fold every fixed_key provider back into its credential: the key returns to
    # ``encrypted_data`` in the codec it was moved out in, and the rule and the
    # policy come back down.
    assignments = ", ".join(f"{col} = p.{col}" for col in POLICY_COLUMNS)
    conn.execute(
        sa.text(
            f"""
            UPDATE managed_ai_credential c
            SET encrypted_data = p.encrypted_secret,
                base_url = p.base_url,
                model = p.model,
                auto_provision_roles = p.auto_provision_roles,
                expiry_notification_date =
                    p.expiry_notification_date AT TIME ZONE 'UTC',
                {assignments},
                provisioning_mode = 'shared',
                provider_id = NULL,
                updated_at = (NOW() AT TIME ZONE 'UTC')
            FROM ai_provider p
            WHERE c.provider_id = p.id AND p.kind = 'fixed_key'
            """
        )
    )
    conn.execute(sa.text("DELETE FROM ai_provider WHERE kind = 'fixed_key'"))

    # Minted providers stay: they are exactly what ``provider_admin_credential``
    # always held. Their credential's mode and rule come back down.
    conn.execute(
        sa.text(
            f"""
            UPDATE managed_ai_credential c
            SET provisioning_mode = 'minted',
                auto_provision_roles = p.auto_provision_roles,
                expiry_notification_date =
                    p.expiry_notification_date AT TIME ZONE 'UTC',
                {assignments},
                updated_at = (NOW() AT TIME ZONE 'UTC')
            FROM ai_provider p
            WHERE c.provider_id = p.id AND p.kind = 'minted'
            """
        )
    )

    # Step 4a's ``0`` branch created an empty credential so the 1:1 invariant
    # would hold, and this does **not** delete it. The shape it produced — a
    # minted parent with a NULL key and no members — is exactly the shape of a
    # pre-existing minted parent nobody has been added to yet, and there is
    # nothing in the row to tell the two apart. Deleting a record an
    # administrator created is worse than leaving one they did not; the leftover
    # is visible and empty, and the admin API deletes it in one press.

    # Split copies (step 4a's ``>1`` branch) are deliberately NOT merged back:
    # nothing records which row was the original, and merging would delete an
    # administration secret that is the only means of revoking the keys minted
    # through it. They survive as ordinary provider_admin_credential rows, which
    # the old schema represents perfectly well.

    # ---- reverse step 3 ---------------------------------------------------
    op.drop_constraint(NEW_PROVIDER_FK, "managed_ai_credential", type_="foreignkey")
    op.alter_column(
        "managed_ai_credential",
        "provider_id",
        new_column_name="provider_admin_credential_id",
    )

    # ---- reverse step 2 ---------------------------------------------------
    op.drop_index("ix_ai_provider_auto_provision", table_name="ai_provider")
    for column in (
        "expiry_notification_date",
        "model_override_building",
        "model_override_conversation",
        "available_models",
        "default_model",
        "sdk_default_modes",
        "set_user_sdk_defaults",
        "set_as_default",
        "auto_provision_roles",
        "model",
        "base_url",
        "kind",
    ):
        op.drop_column("ai_provider", column)

    # ---- reverse step 1 ---------------------------------------------------
    op.execute(
        "ALTER TABLE ai_provider RENAME CONSTRAINT ai_provider_created_by_id_fkey "
        "TO provider_admin_credential_created_by_id_fkey"
    )
    op.execute(
        "ALTER TABLE ai_provider RENAME CONSTRAINT ai_provider_pkey "
        "TO provider_admin_credential_pkey"
    )
    op.execute("ALTER INDEX ix_ai_provider_type RENAME TO ix_provider_admin_credential_type")
    op.rename_table("ai_provider", "provider_admin_credential")

    op.create_foreign_key(
        OLD_PROVIDER_FK,
        "managed_ai_credential",
        "provider_admin_credential",
        ["provider_admin_credential_id"],
        ["id"],
        ondelete="SET NULL",
    )
