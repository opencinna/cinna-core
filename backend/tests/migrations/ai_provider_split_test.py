"""Migration ``c23d6b59a8f5`` — provider_admin_credential → ai_provider.

**Why this group exists at all.** Every other test in this tree talks to the
API, because the API is what has to keep working. A migration has no API: it
runs once, against rows nobody can put there through an endpoint any more (the
``auto_provision_roles`` column it reads is dropped by the very revision under
test), and the way it fails is by *silently* dropping a rule — an admin finds
out weeks later, when a new hire has no key. So this file seeds raw rows into a
scratch database and drives Alembic directly.

The scratch database is created and dropped here. It is never ``app_test``: the
session fixture has already migrated that one to head, and downgrading it under
a running suite would be a good way to lose an afternoon.
"""
import uuid
from datetime import datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.config import settings

#: The revision under test and the one it follows.
BEFORE = "ed8d6a23f13c"
AFTER = "c23d6b59a8f5"

SCRATCH_DB = "app_migration_ai_provider_test"

# Fixed ids so a failure message names something greppable.
PAC_A = uuid.UUID("aaaa0000-0000-0000-0000-000000000001")
MAC_A = uuid.UUID("aaaa1111-0000-0000-0000-000000000001")
PAC_B = uuid.UUID("bbbb0000-0000-0000-0000-000000000001")
MAC_B1 = uuid.UUID("bbbb1111-0000-0000-0000-000000000001")
MAC_B2 = uuid.UUID("bbbb1111-0000-0000-0000-000000000002")
MAC_C = uuid.UUID("cccc1111-0000-0000-0000-000000000001")
MAC_D = uuid.UUID("dddd1111-0000-0000-0000-000000000001")
PAC_E = uuid.UUID("eeee0000-0000-0000-0000-000000000001")
USER = uuid.UUID("0000ffff-0000-0000-0000-000000000001")


def _config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def scratch_url() -> str:
    """A database of its own, migrated to the revision before the one tested."""
    base = str(settings.TEST_SQLALCHEMY_DATABASE_URI).rsplit("/", 1)[0]
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    url = f"{base}/{SCRATCH_DB}"
    command.upgrade(_config(url), BEFORE)
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def scratch(scratch_url: str):
    """An engine on the scratch database, emptied and reset to ``BEFORE``.

    The reset is the point: a test that leaves the database at ``AFTER`` would
    make the next one's ``upgrade`` a no-op that passes without running a line
    of the migration.
    """
    engine = create_engine(scratch_url)
    yield engine
    with engine.begin() as conn:
        # ``ai_provider`` exists only while the database is at ``AFTER``, and a
        # DELETE against a missing table fails at parse time, so the guard has
        # to be here rather than in the statement's WHERE.
        present = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).fetchall()
        }
        for table in (
            "managed_ai_credential_membership",
            "managed_ai_credential",
            "ai_provider",
            "provider_admin_credential",
        ):
            if table in present:
                conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text('DELETE FROM "user" WHERE id = :uid'), {"uid": USER})
        current = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    engine.dispose()
    if current != BEFORE:
        # A test that left the database at ``AFTER`` would make the next one's
        # upgrade a silent no-op.
        command.downgrade(_config(scratch_url), BEFORE)


def _upgrade(url: str) -> None:
    command.upgrade(_config(url), AFTER)


def _downgrade(url: str) -> None:
    command.downgrade(_config(url), BEFORE)


def _exec(engine, sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _rows(engine, sql: str, **params) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(sql), params).fetchall()]


def _seed_user(engine) -> None:
    _exec(
        engine,
        """
        INSERT INTO "user" (
            id, email, is_active, is_superuser, task_sequence_counter, role,
            workspaces_enabled, two_factor_enabled, email_confirmed,
            conversation_style
        ) VALUES (
            :uid, 'migration-fixture@example.com', true, false, 0,
            'agent-user', false, false, true, 'ai_default'
        )
        """,
        uid=USER,
    )


def _seed_all_five_shapes(engine) -> None:
    """The five shapes §12 of the plan names, in one database.

    a) a minted parent on its own admin credential;
    b) two minted parents on one admin credential — the split;
    c) a shared parent *with* auto-provision roles — becomes a fixed_key
       provider;
    d) a shared parent without roles — stays manual, untouched;
    e) an admin credential with no parents — gets an empty managed credential.
    """
    _seed_user(engine)
    _exec(
        engine,
        """
        INSERT INTO provider_admin_credential
            (id, name, provider_type, encrypted_secret, config, created_at, updated_at)
        VALUES
            (:a, 'PAC A', 'openai', 'SECRET-A', '{"project_id":"proj-a"}'::json, NOW(), NOW()),
            (:b, 'PAC B', 'openai', 'SECRET-B', '{"project_id":"proj-b"}'::json, NOW(), NOW()),
            (:e, 'PAC E', 'openai', 'SECRET-E', '{}'::json, NOW(), NOW())
        """,
        a=PAC_A, b=PAC_B, e=PAC_E,
    )
    _exec(
        engine,
        """
        INSERT INTO managed_ai_credential (
            id, name, type, encrypted_data, provisioning_mode,
            provider_admin_credential_id, default_model, available_models,
            set_as_default, set_user_sdk_defaults, sdk_default_modes,
            auto_provision_roles, model_override_conversation,
            expiry_notification_date, created_at, updated_at
        ) VALUES
            (:a, 'MAC A', 'openai', NULL, 'minted', :pac_a, 'gpt-5',
             '["gpt-5"]'::json, true, true, '["conversation"]'::json,
             '["agent-user"]'::json, 'gpt-5-mini', '2027-01-01', NOW(), NOW()),
            (:b1, 'MAC B1', 'openai', NULL, 'minted', :pac_b, NULL, NULL,
             false, false, '["conversation", "building"]'::json, '[]'::json,
             NULL, NULL, '2026-01-01', '2026-01-01'),
            (:b2, 'MAC B2', 'openai', NULL, 'minted', :pac_b, 'gpt-5-mini',
             NULL, true, false, '["building"]'::json, '["admin"]'::json, NULL,
             NULL, '2026-02-01', '2026-02-01'),
            (:c, 'MAC C', 'anthropic', 'ENCRYPTED-C', 'shared', NULL,
             'claude-sonnet-4-6', '["claude-sonnet-4-6"]'::json, false, true,
             '["conversation", "building"]'::json, '["agent-developer"]'::json,
             'claude-haiku-4', '2026-12-31', NOW(), NOW()),
            (:d, 'MAC D', 'anthropic', 'ENCRYPTED-D', 'shared', NULL, NULL,
             NULL, true, true, '["conversation", "building"]'::json,
             '[]'::json, NULL, NULL, NOW(), NOW())
        """,
        a=MAC_A, b1=MAC_B1, b2=MAC_B2, c=MAC_C, d=MAC_D,
        pac_a=PAC_A, pac_b=PAC_B,
    )
    _exec(
        engine,
        """
        INSERT INTO managed_ai_credential_membership
            (id, managed_credential_id, user_id, status, provision_attempts,
             created_at, updated_at)
        VALUES (gen_random_uuid(), :c, :uid, 'not_applicable', 0, NOW(), NOW())
        """,
        c=MAC_C, uid=USER,
    )


# --------------------------------------------------------------------------- #
# Up and down against all five shapes
# --------------------------------------------------------------------------- #


def test_the_five_shapes_survive_up_then_down_then_up(scratch, scratch_url):
    """Each shape lands where §3 says, comes back, and lands there again.

    The second upgrade is not decoration. The first one is the only one that
    ever runs against pre-feature data; the second runs against whatever the
    downgrade left behind, and that is the state an operator who rolls back and
    forward again actually has.
    """
    _seed_all_five_shapes(scratch)

    _upgrade(scratch_url)

    providers = dict(
        (name, (kind, roles))
        for name, kind, roles in _rows(
            scratch, "SELECT name, kind, auto_provision_roles FROM ai_provider"
        )
    )
    # (a) and (e) keep their provider; (b) split into two; (c) gained one.
    assert set(providers) == {
        "PAC A", "PAC B", "PAC B — MAC B2", "PAC E", "MAC C",
    }
    assert providers["PAC A"] == ("minted", ["agent-user"])
    assert providers["PAC B"] == ("minted", [])
    assert providers["PAC B — MAC B2"] == ("minted", ["admin"])
    assert providers["MAC C"] == ("fixed_key", ["agent-developer"])

    # (c) The key moved to the provider and only the provider.
    assert _rows(
        scratch,
        "SELECT p.encrypted_secret, c.encrypted_data FROM managed_ai_credential c "
        "JOIN ai_provider p ON p.id = c.provider_id WHERE c.id = :c",
        c=MAC_C,
    ) == [("ENCRYPTED-C", None)]

    # (c) The wiring policy came up with it, whole.
    assert _rows(
        scratch,
        "SELECT set_as_default, set_user_sdk_defaults, sdk_default_modes, "
        "default_model, available_models, model_override_conversation "
        "FROM ai_provider WHERE name = 'MAC C'",
    ) == [(
        False, True, ["conversation", "building"], "claude-sonnet-4-6",
        ["claude-sonnet-4-6"], "claude-haiku-4",
    )]

    # (b) The split repointed the second credential and copied the secret.
    assert _rows(
        scratch,
        "SELECT p.name, p.encrypted_secret FROM managed_ai_credential c "
        "JOIN ai_provider p ON p.id = c.provider_id WHERE c.id = :b2",
        b2=MAC_B2,
    ) == [("PAC B — MAC B2", "SECRET-B")]

    # (d) A shared parent with no rule is manual and was not touched.
    assert _rows(
        scratch,
        "SELECT provider_id, encrypted_data FROM managed_ai_credential WHERE id = :d",
        d=MAC_D,
    ) == [(None, "ENCRYPTED-D")]

    # (e) The empty provider got the one credential the 1:1 invariant needs.
    assert _rows(
        scratch,
        "SELECT COUNT(*) FROM managed_ai_credential c "
        "JOIN ai_provider p ON p.id = c.provider_id WHERE p.name = 'PAC E'",
    ) == [(1,)]

    # The provider's column is timestamptz and the credential's is a naive
    # timestamp, so the migration converts explicitly in both directions. An
    # implicit cast would shift the value by the session time zone, silently.
    assert _rows(
        scratch,
        "SELECT expiry_notification_date AT TIME ZONE 'UTC' FROM ai_provider "
        "WHERE name = 'MAC C'",
    ) == [(datetime(2026, 12, 31, 0, 0),)]

    _downgrade(scratch_url)

    assert _rows(
        scratch,
        "SELECT expiry_notification_date FROM managed_ai_credential WHERE id = :c",
        c=MAC_C,
    ) == [(datetime(2026, 12, 31, 0, 0),)]

    # (c) came back whole: key, rule and policy are the credential's again.
    assert _rows(
        scratch,
        "SELECT provisioning_mode, provider_admin_credential_id, encrypted_data, "
        "auto_provision_roles, set_user_sdk_defaults, default_model, "
        "model_override_conversation FROM managed_ai_credential WHERE id = :c",
        c=MAC_C,
    ) == [(
        "shared", None, "ENCRYPTED-C", ["agent-developer"], True,
        "claude-sonnet-4-6", "claude-haiku-4",
    )]
    # (a) came back minted, with its rule.
    assert _rows(
        scratch,
        "SELECT provisioning_mode, provider_admin_credential_id, "
        "auto_provision_roles FROM managed_ai_credential WHERE id = :a",
        a=MAC_A,
    ) == [("minted", PAC_A, ["agent-user"])]
    # (b) Both credentials survive the round trip. The split copy stays — see
    # the migration's docstring for why merging it back is refused.
    assert sorted(
        name for (name,) in _rows(scratch, "SELECT name FROM managed_ai_credential")
    ) == ["MAC A", "MAC B1", "MAC B2", "MAC C", "MAC D", "PAC E"]

    _upgrade(scratch_url)

    assert sorted(
        name for (name,) in _rows(scratch, "SELECT name FROM ai_provider")
    ) == ["MAC C", "PAC A", "PAC B", "PAC B — MAC B2", "PAC E"]


def test_every_provider_owns_exactly_one_credential_after_the_upgrade(
    scratch, scratch_url
):
    """The 1:1 invariant the split, the empty-provider branch and the delete
    ordering all rest on. Asserted from the data rather than trusted from the
    branch that was supposed to establish it."""
    _seed_all_five_shapes(scratch)
    _upgrade(scratch_url)

    assert _rows(
        scratch,
        """
        SELECT p.id, COUNT(c.id) FROM ai_provider p
        LEFT JOIN managed_ai_credential c ON c.provider_id = p.id
        GROUP BY p.id HAVING COUNT(c.id) <> 1
        """,
    ) == []


def test_a_provider_cannot_be_deleted_out_from_under_its_credential(
    scratch, scratch_url
):
    """RESTRICT, not SET NULL — §3.2. A nulled pointer would silently turn a
    provider-owned credential into a manual one holding stale policy."""
    import sqlalchemy.exc

    _seed_all_five_shapes(scratch)
    _upgrade(scratch_url)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _exec(scratch, "DELETE FROM ai_provider WHERE name = 'PAC A'")


# --------------------------------------------------------------------------- #
# Fail loudly, one case per unclassifiable shape
# --------------------------------------------------------------------------- #


def _shared(**overrides) -> str:
    """A ``shared`` managed credential insert with the given columns replaced."""
    values = {
        "encrypted_data": "'K'",
        "provisioning_mode": "'shared'",
        "provider_admin_credential_id": "NULL",
        "auto_provision_roles": "'[]'::json",
        "type": "'anthropic'",
    }
    values.update(overrides)
    return f"""
        INSERT INTO managed_ai_credential (
            id, name, type, encrypted_data, provisioning_mode,
            provider_admin_credential_id, set_as_default, set_user_sdk_defaults,
            sdk_default_modes, auto_provision_roles, created_at, updated_at
        ) VALUES (
            :id, 'unclassifiable', {values['type']}, {values['encrypted_data']},
            {values['provisioning_mode']},
            {values['provider_admin_credential_id']}, false, false,
            '["conversation", "building"]'::json,
            {values['auto_provision_roles']}, NOW(), NOW()
        )
    """


BAD_ID = uuid.UUID("dead0000-0000-0000-0000-000000000001")


def test_a_role_list_that_is_not_a_list_aborts_the_migration(scratch, scratch_url):
    _exec(scratch, _shared(auto_provision_roles="'\"agent-user\"'::json"), id=BAD_ID)
    with pytest.raises(RuntimeError, match=str(BAD_ID)):
        _upgrade(scratch_url)


def test_a_rule_with_no_key_behind_it_aborts_the_migration(scratch, scratch_url):
    """A credential that hands itself out but holds nothing to hand out. There
    is no key source to turn into a provider, and dropping the rule quietly is
    how a company key stops reaching new hires."""
    _exec(
        scratch,
        _shared(
            encrypted_data="NULL",
            auto_provision_roles="'[\"agent-user\"]'::json",
        ),
        id=BAD_ID,
    )
    with pytest.raises(RuntimeError, match=str(BAD_ID)):
        _upgrade(scratch_url)


def test_a_minted_credential_with_nothing_to_mint_through_aborts(scratch, scratch_url):
    _exec(
        scratch,
        _shared(
            type="'openai'",
            encrypted_data="NULL",
            provisioning_mode="'minted'",
        ),
        id=BAD_ID,
    )
    with pytest.raises(RuntimeError, match=str(BAD_ID)):
        _upgrade(scratch_url)


def test_a_shared_credential_pointing_at_an_admin_secret_aborts(scratch, scratch_url):
    _exec(
        scratch,
        """
        INSERT INTO provider_admin_credential
            (id, name, provider_type, encrypted_secret, config, created_at, updated_at)
        VALUES (:pac, 'PAC', 'openai', 'S', '{}'::json, NOW(), NOW())
        """,
        pac=PAC_A,
    )
    _exec(
        scratch,
        _shared(type="'openai'", provider_admin_credential_id=":pac"),
        id=BAD_ID,
        pac=PAC_A,
    )
    with pytest.raises(RuntimeError, match=str(BAD_ID)):
        _upgrade(scratch_url)


def test_a_type_no_adapter_serves_aborts_the_migration(scratch, scratch_url):
    """Asked of the adapter registry, not of a list written into the migration,
    so the check cannot drift from the adapters."""
    _exec(
        scratch,
        """
        INSERT INTO provider_admin_credential
            (id, name, provider_type, encrypted_secret, config, created_at, updated_at)
        VALUES (:pac, 'gone', 'a_provider_we_never_had', 'S', '{}'::json, NOW(), NOW())
        """,
        pac=BAD_ID,
    )
    with pytest.raises(RuntimeError, match=str(BAD_ID)):
        _upgrade(scratch_url)


def test_the_downgrade_refuses_a_provider_it_has_no_record_of(scratch, scratch_url):
    """A ``fixed_key`` provider with no rule cannot have come from step 4b, so
    it postdates the upgrade. Folding it into a manual credential would leave
    its members holding a key from a configuration that vanished."""
    _seed_all_five_shapes(scratch)
    _upgrade(scratch_url)

    _exec(
        scratch,
        """
        INSERT INTO ai_provider (
            id, name, kind, provider_type, encrypted_secret, config,
            auto_provision_roles, set_as_default, set_user_sdk_defaults,
            sdk_default_modes, created_at, updated_at
        ) VALUES (
            :pid, 'made later', 'fixed_key', 'anthropic', 'K2', '{}'::json,
            '[]'::json, false, false, '["conversation"]'::json, NOW(), NOW()
        )
        """,
        pid=BAD_ID,
    )
    _exec(
        scratch,
        """
        INSERT INTO managed_ai_credential (
            id, name, type, encrypted_data, provider_id, set_as_default,
            set_user_sdk_defaults, sdk_default_modes, created_at, updated_at
        ) VALUES (
            :cid, 'made later', 'anthropic', NULL, :pid, false, false,
            '["conversation"]'::json, NOW(), NOW()
        )
        """,
        cid=uuid.UUID("dead1111-0000-0000-0000-000000000001"), pid=BAD_ID,
    )
    _exec(
        scratch,
        """
        INSERT INTO managed_ai_credential_membership
            (id, managed_credential_id, user_id, status, provision_attempts,
             created_at, updated_at)
        VALUES (gen_random_uuid(), :cid, :uid, 'not_applicable', 0, NOW(), NOW())
        """,
        cid=uuid.UUID("dead1111-0000-0000-0000-000000000001"), uid=USER,
    )

    with pytest.raises(RuntimeError, match=str(BAD_ID)):
        _downgrade(scratch_url)
