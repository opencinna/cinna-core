"""Structural drift test — `ChannelUserSetting`'s nullable-meaning-inherit columns.

WHY THIS EXISTS
---------------
The model's own docstring (`app/models/server_channels/channel_user_setting.py`)
calls "tidying" `is_enabled` / `agent_scope` into `NOT NULL DEFAULT ...` "the
single most damaging change that can be made to this table": a stored value
FREEZES the user against a later change of the admin default, because
`ChannelPolicyService.resolve` reads NULL as "inherit" and anything else as an
explicit choice. Phase-2 plan §11 names this as the headline trap of the whole
migration and, until now, it was enforced only by that prose — a reader could
"fix" the columns into a more conventional shape, the suite would stay green
(nothing in `tests/api/` exercises column-level nullability; SQLModel does not
validate `table=True` classes on construction either), and the inherit
guarantee would silently stop holding for every future admin default change.

This is a structural / drift test in the sense `tests/README.md`'s
"Architecture Tests" section describes: it inspects `app` metadata directly
rather than exercising it through the API, so Rule 1 (no `app.services` /
`app.crud` imports in `tests/api/`) does not apply here — see
`channel_routing_purity_test.py` for the sibling technique (AST inspection of
source) applied to a different invariant. This file inspects the SQLAlchemy
`Table` object SQLModel builds from the class declaration instead — cheaper
than a migration round-trip and independent of whatever the currently-applied
alembic revision happens to say, which is the point: this is a claim about
the MODEL, the thing `ChannelPolicyService` actually reads at runtime, not
about one migration's autogenerate output.

WHAT IT ENFORCES
----------------
- `is_enabled` and `agent_scope`: `nullable is True` and `server_default is
  None`. Both conditions matter independently — a column that is nullable but
  carries a server default would still let every fresh row default to a
  non-NULL value at the database level, which is the same freeze bug by a
  different route.
- `allow_identity_routing`: `nullable is False` — this column does NOT inherit
  (master plan §3.4) and NULL must never be a way to skip that opt-in.

WHY `.default` IS NOT ALSO ASSERTED FOR `allow_identity_routing`
------------------------------------------------------------------
`ChannelUserSetting.allow_identity_routing: bool = Field(default=False)`
gives the column a Python-side `.default` (applied by SQLAlchemy at INSERT
time), not a `.server_default` (a DDL-level DEFAULT clause the database
itself applies). The migration's `ADD COLUMN ... server_default=sa.false()`
exists only so Alembic can backfill this NOT NULL column onto the existing
table's rows; it is a fact about that one migration script, not about the
model `ChannelPolicyService` reads. Asserting `.server_default is not None`
here would pin a property this file has no way to keep true — the model
declares no `sa_column=Column(..., server_default=...)`, so nothing
regenerates that DDL-level default if the column is ever dropped and re-added
by a future migration. `.default is not None` is the property the model
actually promises and is what is checked.
"""
from app.models import ChannelUserSetting


def test_is_enabled_is_nullable_with_no_server_default() -> None:
    """NULL means "inherit `ServerChannel.default_enabled_for_users`". A
    `server_default` here would mean every fresh row defaults to a concrete,
    user-owned value at the database level — the freeze bug, from SQL instead
    of from the ORM.
    """
    column = ChannelUserSetting.__table__.c.is_enabled
    assert column.nullable is True, (
        "channel_user_setting.is_enabled must stay nullable: NULL is how a "
        "row says 'follow the channel default'. See ChannelUserSetting's "
        "module docstring before changing this."
    )
    assert column.server_default is None, (
        "channel_user_setting.is_enabled must carry no server_default: a "
        "default value would freeze every fresh row against a later admin "
        "default change, defeating the inherit rule at the database level."
    )


def test_agent_scope_is_nullable_with_no_server_default() -> None:
    """Same rule as `is_enabled`, for `ServerChannel.default_agent_scope`."""
    column = ChannelUserSetting.__table__.c.agent_scope
    assert column.nullable is True, (
        "channel_user_setting.agent_scope must stay nullable: NULL is how a "
        "row says 'follow the channel default'. See ChannelUserSetting's "
        "module docstring before changing this."
    )
    assert column.server_default is None, (
        "channel_user_setting.agent_scope must carry no server_default: a "
        "default value would freeze every fresh row against a later admin "
        "default change, defeating the inherit rule at the database level."
    )


def test_allow_identity_routing_is_not_nullable_and_has_a_default() -> None:
    """The deliberate exception: this column never inherits (master plan
    §3.4), so unlike its two neighbours it must be `NOT NULL` — NULL must
    never be a way to accidentally opt into routing a message into somebody
    else's workspace. A concrete Python-side default is what lets every
    write that omits it land the safe value (`False`) rather than requiring
    every caller to spell it out.
    """
    column = ChannelUserSetting.__table__.c.allow_identity_routing
    assert column.nullable is False, (
        "channel_user_setting.allow_identity_routing must be NOT NULL: this "
        "column has no channel-level default to inherit from (master plan "
        "§3.4), so NULL cannot be a way to route into another person's "
        "workspace without having opted in."
    )
    assert column.default is not None, (
        "channel_user_setting.allow_identity_routing must carry a Python-side "
        "default (Field(default=False)) so an insert that omits it lands the "
        "safe value rather than failing the NOT NULL constraint."
    )
