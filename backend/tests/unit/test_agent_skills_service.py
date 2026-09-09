"""Unit tests: the agent skills cache service.

Pure-logic coverage of the parts that decide what reaches the card: how a
reported index is normalised into cached rows, when the cache is considered
changed (which is what gates the ``AGENT_UPDATED`` emit), and how the cached
rows are read back as ``SkillEntry`` objects — including a row written in the
pre-``{code, message}`` shape, which a live cache can still hold until its next
refresh.

No DB and no adapter: ``AgentEnvironment`` is constructed in memory, which is
enough because every method under test reads the row and nothing else.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.environments.environment import AgentEnvironment
from app.services.agents.agent_skills_service import (
    ERROR_ADAPTER_ERROR,
    ERROR_ADAPTER_UNSUPPORTED,
    ERROR_ENV_NOT_RUNNING,
    SLEEPING_STATUSES,
    AgentSkillsService,
    SkillsIndexUnavailableError,
)
from app.services.environments.adapters.base import EndpointUnsupportedError


def _environment(**overrides) -> AgentEnvironment:
    base = dict(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        env_name="test-env",
        status="running",
    )
    base.update(overrides)
    return AgentEnvironment(**base)


class _FakeSession:
    """The two calls ``_persist`` / ``_persist_error`` make, and a commit count.

    Deliberately not the real ``db`` fixture: everything under test here reads
    and writes one row, so a fake keeps these tests DB-free (and out of the
    migration-ordering trap the service-level tests in this directory carry).
    """

    def __init__(self, environment: AgentEnvironment):
        self._environment = environment
        self.commits = 0

    def get(self, _model, identifier):
        return self._environment if identifier == self._environment.id else None

    def add(self, _instance):
        pass

    def commit(self):
        self.commits += 1


class _FakeAdapter:
    def __init__(self, payload=None, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.calls = 0

    async def get_skills_index(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._payload


@pytest.fixture
def stub_adapter(monkeypatch):
    """Point the service at a fake adapter and swallow the event emit."""

    def _install(adapter):
        from app.services.environments import environment_service

        class _Lifecycle:
            def get_adapter(self, _environment):
                return adapter

        monkeypatch.setattr(
            environment_service.EnvironmentService,
            "get_lifecycle_manager",
            staticmethod(lambda: _Lifecycle()),
        )
        emitted: list[int] = []
        monkeypatch.setattr(
            AgentSkillsService,
            "_fire_agent_updated",
            classmethod(lambda cls, env, skill_count: emitted.append(skill_count)),
        )
        return emitted

    return _install


def _payload(entries, tree_hash="hash-1"):
    return {"hash": tree_hash, "skills": entries, "errors": []}


def _reported(**overrides) -> dict:
    entry = {
        "name": "pdf-report",
        "description": "Build a PDF report.",
        "source": "local",
        "plugin_ref": None,
        "path": "skills/pdf-report",
        "has_scripts": True,
        "user_invocable": True,
        "model_invocable": True,
        "size_bytes": 4096,
        "error": None,
        "warning": None,
        "secret_paths": [],
        "version": None,
    }
    entry.update(overrides)
    return entry


class TestNormaliseEntries:

    def test_a_reported_entry_round_trips(self):
        rows = AgentSkillsService._normalise_entries([_reported()])
        assert rows == [_reported()]

    def test_non_list_and_non_dict_items_are_dropped(self):
        assert AgentSkillsService._normalise_entries("nope") == []
        assert AgentSkillsService._normalise_entries([1, None, "x"]) == []

    def test_entry_without_a_name_is_dropped(self):
        rows = AgentSkillsService._normalise_entries(
            [_reported(name=""), _reported()]
        )
        assert [r["name"] for r in rows] == ["pdf-report"]

    def test_missing_fields_get_safe_defaults(self):
        rows = AgentSkillsService._normalise_entries([{"name": "bare"}])
        assert rows[0]["path"] == "skills/bare"
        assert rows[0]["source"] == "local"
        assert rows[0]["size_bytes"] == 0
        assert rows[0]["secret_paths"] == []

    def test_issue_shapes_are_preserved(self):
        rows = AgentSkillsService._normalise_entries(
            [
                _reported(
                    warning={
                        "code": "secrets",
                        "message": "This skill holds files that look like credentials.",
                        "paths": [".env"],
                    },
                    secret_paths=[".env"],
                )
            ]
        )
        assert rows[0]["warning"]["code"] == "secrets"
        assert rows[0]["warning"]["paths"] == [".env"]
        assert rows[0]["secret_paths"] == [".env"]

    def test_a_bare_code_string_is_upgraded_to_the_issue_shape(self):
        # What a cache row written before ``{code, message}`` existed looks like.
        rows = AgentSkillsService._normalise_entries([_reported(error="budget")])
        assert rows[0]["error"]["code"] == "budget"
        assert rows[0]["error"]["message"]

    def test_the_list_is_capped(self):
        from app.services.agents.agent_skills_service import MAX_CACHED_SKILLS

        reported = [_reported(name=f"skill-{i}") for i in range(MAX_CACHED_SKILLS + 100)]

        rows = AgentSkillsService._normalise_entries(reported)

        assert len(rows) == MAX_CACHED_SKILLS


class TestEntriesDiffer:

    def test_first_ever_index_with_skills_is_a_change(self):
        assert AgentSkillsService._entries_differ(None, [_reported()]) is True

    def test_first_ever_index_with_no_skills_is_not_a_change(self):
        # Nothing to re-render for: the card showed nothing and still shows
        # nothing, so no client is woken.
        assert AgentSkillsService._entries_differ(None, []) is False

    def test_identical_lists_are_not_a_change(self):
        rows = AgentSkillsService._normalise_entries([_reported()])
        assert AgentSkillsService._entries_differ(rows, rows) is False

    def test_a_changed_description_is_a_change(self):
        before = AgentSkillsService._normalise_entries([_reported()])
        after = AgentSkillsService._normalise_entries(
            [_reported(description="Different.")]
        )
        assert AgentSkillsService._entries_differ(before, after) is True


class TestCachedReads:

    def test_no_cache_reads_as_empty(self):
        assert AgentSkillsService.get_cached(_environment()) == []
        assert AgentSkillsService.get_cached(None) == []
        assert AgentSkillsService.get_cached_entries(_environment()) == []

    def test_cached_rows_read_back_as_skill_entries(self):
        env = _environment(
            skills_parsed=AgentSkillsService._normalise_entries(
                [
                    _reported(),
                    _reported(
                        name="broken",
                        error={"code": "name_mismatch", "message": "Nope.", "paths": []},
                    ),
                ]
            )
        )

        entries = AgentSkillsService.get_cached_entries(env)

        assert [e.name for e in entries] == ["pdf-report", "broken"]
        assert entries[0].is_valid is True
        assert entries[0].is_publishable is True
        assert entries[1].is_valid is False
        assert entries[1].error.code == "name_mismatch"

    def test_a_secret_bearing_skill_reads_back_as_not_publishable(self):
        env = _environment(
            skills_parsed=AgentSkillsService._normalise_entries(
                [_reported(secret_paths=["scripts/.env"])]
            )
        )

        entry = AgentSkillsService.get_cached_entries(env)[0]

        assert entry.is_valid is True       # still projected — the skill works
        assert entry.is_publishable is False  # but never shipped

    def test_rate_limit_bucket_is_independent_of_other_caches(self):
        from app.services.agents.cli_commands_service import CLICommandsService

        env_id = uuid.uuid4()
        AgentSkillsService._mark_rate_limit(env_id)

        assert AgentSkillsService.is_rate_limited(env_id) is True
        assert CLICommandsService.is_rate_limited(env_id) is False


class TestFetchIndex:
    """The write short-circuit, the event rule and the failure classification."""

    @pytest.mark.anyio
    async def test_first_fetch_persists_and_emits(self, stub_adapter):
        env = _environment()
        adapter = _FakeAdapter(_payload([_reported()]))
        emitted = stub_adapter(adapter)
        session = _FakeSession(env)

        entries = await AgentSkillsService.fetch_index(env, db_session=session)

        assert [e["name"] for e in entries] == ["pdf-report"]
        assert env.skills_hash == "hash-1"
        assert env.skills_error is None
        assert env.skills_fetched_at is not None
        assert session.commits == 1
        assert emitted == [1]

    @pytest.mark.anyio
    async def test_unchanged_index_writes_nothing_and_emits_nothing(
        self, stub_adapter
    ):
        env = _environment()
        adapter = _FakeAdapter(_payload([_reported()]))
        emitted = stub_adapter(adapter)
        session = _FakeSession(env)

        await AgentSkillsService.fetch_index(env, db_session=session)
        AgentSkillsService._mark_rate_limit(uuid.uuid4())  # unrelated bucket
        await AgentSkillsService.fetch_index(env, db_session=session)

        assert adapter.calls == 2      # we still ask
        assert session.commits == 1    # but write only once
        assert emitted == [1]          # and notify only once

    @pytest.mark.anyio
    async def test_a_plugin_skill_appearing_is_not_discarded(self, stub_adapter):
        # Regression: the short-circuit used to key off the reported tree hash,
        # which covers the workspace skills/ folder ONLY. Installing a plugin
        # that ships skills changes the index while leaving the hash fixed —
        # and for an agent with no local skills the hash never moves at all.
        env = _environment()
        local_only = _payload([_reported()], tree_hash="stable")
        with_plugin = _payload(
            [
                _reported(),
                _reported(name="from-plugin", source="plugin", plugin_ref="mkt/p"),
            ],
            tree_hash="stable",
        )
        adapter = _FakeAdapter(local_only)
        emitted = stub_adapter(adapter)
        session = _FakeSession(env)

        await AgentSkillsService.fetch_index(env, db_session=session)
        adapter._payload = with_plugin
        entries = await AgentSkillsService.fetch_index(env, db_session=session)

        assert [e["name"] for e in entries] == ["pdf-report", "from-plugin"]
        assert [e["name"] for e in env.skills_parsed] == ["pdf-report", "from-plugin"]
        assert emitted == [1, 2]

    @pytest.mark.anyio
    async def test_a_running_env_that_fails_is_an_adapter_error(self, stub_adapter):
        env = _environment(status="running")
        stub_adapter(_FakeAdapter(error=RuntimeError("boom")))
        session = _FakeSession(env)

        with pytest.raises(SkillsIndexUnavailableError):
            await AgentSkillsService.fetch_index(env, db_session=session)

        assert env.skills_error == ERROR_ADAPTER_ERROR

    @pytest.mark.anyio
    async def test_a_sleeping_env_that_fails_is_env_not_running(self, stub_adapter):
        env = _environment(status="suspended")
        stub_adapter(_FakeAdapter(error=RuntimeError("connection refused")))
        session = _FakeSession(env)

        with pytest.raises(SkillsIndexUnavailableError):
            await AgentSkillsService.fetch_index(env, db_session=session)

        assert env.skills_error == ERROR_ENV_NOT_RUNNING

    @pytest.mark.anyio
    async def test_a_container_with_no_such_route_is_adapter_unsupported(
        self, stub_adapter
    ):
        # The container answered — it simply has no ``/config/skills``. That is
        # a rebuild, not a restart, and the reason code is the only thing that
        # tells the card and the CLI which of the two to name.
        env = _environment(status="running")
        stub_adapter(_FakeAdapter(error=EndpointUnsupportedError("/config/skills")))
        session = _FakeSession(env)

        with pytest.raises(SkillsIndexUnavailableError) as excinfo:
            await AgentSkillsService.fetch_index(env, db_session=session)

        assert env.skills_error == ERROR_ADAPTER_UNSUPPORTED
        # The reason travels on the exception too — callers that never touch
        # the row (the CLI, the refresh route) read it from there.
        assert str(excinfo.value).startswith(ERROR_ADAPTER_UNSUPPORTED)

    @pytest.mark.anyio
    @pytest.mark.parametrize("sleeping", sorted(SLEEPING_STATUSES))
    async def test_unsupported_outranks_a_sleeping_environment(
        self, stub_adapter, sleeping
    ):
        # THE ordering guard. ``EndpointUnsupportedError`` is classified BEFORE
        # the status branch on purpose: a pre-feature container that also
        # happens to be suspended still needs a rebuild, and "refresh to wake
        # it" walks the user around a loop the wake can never break. Move the
        # branch below the status check and this is the test that fails.
        env = _environment(status=sleeping)
        stub_adapter(_FakeAdapter(error=EndpointUnsupportedError("/config/skills")))
        session = _FakeSession(env)

        with pytest.raises(SkillsIndexUnavailableError):
            await AgentSkillsService.fetch_index(env, db_session=session)

        assert env.skills_error == ERROR_ADAPTER_UNSUPPORTED, (
            f"a {sleeping} container that predates the route still needs a "
            "rebuild — 'wake it and refresh' is the one remedy that cannot work"
        )

    @pytest.mark.anyio
    async def test_a_non_404_http_failure_is_still_an_adapter_error(
        self, stub_adapter
    ):
        # The exception the docker adapter raises for a 500: a container that
        # HAS the endpoint and failed inside it. Sharing a code with the
        # unsupported case is what made the card tell an unreachable
        # environment to rebuild.
        env = _environment(status="running")
        stub_adapter(
            _FakeAdapter(
                error=Exception(
                    "Failed to get skills index: Server error '500 Internal "
                    "Server Error' for url 'http://agent-x:8000/config/skills'"
                )
            )
        )
        session = _FakeSession(env)

        with pytest.raises(SkillsIndexUnavailableError):
            await AgentSkillsService.fetch_index(env, db_session=session)

        assert env.skills_error == ERROR_ADAPTER_ERROR
        assert env.skills_error != ERROR_ADAPTER_UNSUPPORTED

    @pytest.mark.anyio
    async def test_an_unsupported_read_that_starts_answering_clears_the_banner(
        self, stub_adapter
    ):
        # A rebuild is the fix, and after it the code must not survive — a
        # sticky ``adapter_unsupported`` would keep asking for the rebuild
        # that already happened.
        env = _environment(status="running")
        adapter = _FakeAdapter(error=EndpointUnsupportedError("/config/skills"))
        stub_adapter(adapter)
        session = _FakeSession(env)

        with pytest.raises(SkillsIndexUnavailableError):
            await AgentSkillsService.fetch_index(env, db_session=session)
        assert env.skills_error == ERROR_ADAPTER_UNSUPPORTED

        adapter._error = None
        adapter._payload = _payload([_reported()])
        await AgentSkillsService.fetch_index(env, db_session=session)

        assert env.skills_error is None
        assert [e["name"] for e in env.skills_parsed] == ["pdf-report"]

    @pytest.mark.anyio
    async def test_the_adapter_is_called_even_while_the_env_is_still_activating(
        self, stub_adapter
    ):
        # The env-start sweep runs inside _sync_dynamic_data, BEFORE the row is
        # stamped "running". A status pre-check here would make that sweep dead
        # code and persist "asleep" about a container that just came up.
        env = _environment(status="activating")
        adapter = _FakeAdapter(_payload([_reported()]))
        stub_adapter(adapter)
        session = _FakeSession(env)

        entries = await AgentSkillsService.fetch_index(env, db_session=session)

        assert adapter.calls == 1
        assert [e["name"] for e in entries] == ["pdf-report"]
        assert env.skills_error is None

    @pytest.mark.anyio
    async def test_a_persisted_error_is_cleared_by_the_next_good_read(
        self, stub_adapter
    ):
        env = _environment(
            skills_parsed=AgentSkillsService._normalise_entries([_reported()]),
            skills_hash="hash-1",
            skills_error=ERROR_ENV_NOT_RUNNING,
        )
        adapter = _FakeAdapter(_payload([_reported()]))
        stub_adapter(adapter)
        session = _FakeSession(env)

        await AgentSkillsService.fetch_index(env, db_session=session)

        # Identical content, but the error must not survive — otherwise the
        # card keeps its banner forever.
        assert env.skills_error is None
        assert session.commits == 1

    @pytest.mark.anyio
    async def test_a_non_object_payload_is_a_parse_error(self, stub_adapter):
        env = _environment()
        stub_adapter(_FakeAdapter(["not", "an", "object"]))
        session = _FakeSession(env)

        with pytest.raises(SkillsIndexUnavailableError):
            await AgentSkillsService.fetch_index(env, db_session=session)

        assert env.skills_error == "parse_error"


class TestRefreshTriggers:

    @pytest.mark.anyio
    async def test_refresh_after_action_never_raises(self, stub_adapter):
        env = _environment()
        stub_adapter(_FakeAdapter(error=RuntimeError("down")))

        await AgentSkillsService.refresh_after_action(
            env, db_session=_FakeSession(env), force=True
        )  # must not raise

    @pytest.mark.anyio
    async def test_the_rate_limit_is_bypassed_by_force(self, stub_adapter):
        env = _environment()
        adapter = _FakeAdapter(_payload([_reported()]))
        stub_adapter(adapter)
        session = _FakeSession(env)
        AgentSkillsService._mark_rate_limit(env.id)

        await AgentSkillsService.refresh_after_action(env, db_session=session)
        assert adapter.calls == 0

        await AgentSkillsService.refresh_after_action(
            env, db_session=session, force=True
        )
        assert adapter.calls == 1

    def test_a_watcher_signal_naming_the_skills_folder_forces(self):
        from app.services.agents.agent_skills_service import SKILLS_DIR_PATH
        from app.services.environments.synced_files import SYNCED_FILES

        # The constant the handler compares ``changed_files`` against IS the
        # registry's rel_path — env-core reports exactly that string.
        registered = next(f.rel_path for f in SYNCED_FILES if f.key == "skills")
        assert SKILLS_DIR_PATH == registered == "skills/"


class TestPostActionEvents:
    """``handle_post_action_event`` — the one entry point every event uses.

    The handler opens its own session (the event bus hands it a dict, not a
    session), so these tests patch ``create_session`` to yield the same fake
    row the rest of this file uses. What is under test is the DECISION the
    handler makes from the event meta: which environment, and whether the
    trigger is direct evidence (force) or a guess (rate-limited).
    """

    @staticmethod
    def _patch_session(monkeypatch, session):
        import contextlib

        from app.core import db as db_module

        @contextlib.contextmanager
        def _factory():
            yield session

        monkeypatch.setattr(db_module, "create_session", _factory)

    @pytest.mark.anyio
    async def test_an_event_without_an_environment_is_ignored(
        self, stub_adapter, monkeypatch
    ):
        env = _environment()
        adapter = _FakeAdapter(_payload([_reported()]))
        stub_adapter(adapter)
        session = _FakeSession(env)
        self._patch_session(monkeypatch, session)

        await AgentSkillsService.handle_post_action_event({"meta": {}})
        await AgentSkillsService.handle_post_action_event({})

        assert adapter.calls == 0

    @pytest.mark.anyio
    async def test_an_event_for_an_unknown_environment_is_ignored(
        self, stub_adapter, monkeypatch
    ):
        env = _environment()
        adapter = _FakeAdapter(_payload([_reported()]))
        stub_adapter(adapter)
        self._patch_session(monkeypatch, _FakeSession(env))

        await AgentSkillsService.handle_post_action_event(
            {"meta": {"environment_id": str(uuid.uuid4())}}
        )

        assert adapter.calls == 0

    @pytest.mark.anyio
    async def test_a_stream_completing_refreshes_the_cache(
        self, stub_adapter, monkeypatch
    ):
        # STREAM_COMPLETED carries no changed_files — the speculative trigger.
        env = _environment()
        adapter = _FakeAdapter(_payload([_reported()]))
        stub_adapter(adapter)
        session = _FakeSession(env)
        self._patch_session(monkeypatch, session)

        await AgentSkillsService.handle_post_action_event(
            {"meta": {"environment_id": str(env.id)}}
        )

        assert adapter.calls == 1
        assert [e["name"] for e in env.skills_parsed] == ["pdf-report"]

    @pytest.mark.anyio
    async def test_a_watcher_signal_naming_skills_bypasses_the_rate_limit(
        self, stub_adapter, monkeypatch
    ):
        from app.services.agents.agent_skills_service import SKILLS_DIR_PATH

        env = _environment()
        adapter = _FakeAdapter(_payload([_reported()]))
        stub_adapter(adapter)
        session = _FakeSession(env)
        self._patch_session(monkeypatch, session)
        AgentSkillsService._mark_rate_limit(env.id)

        # A different watched file inside the window: still a guess.
        await AgentSkillsService.handle_post_action_event(
            {
                "meta": {
                    "environment_id": str(env.id),
                    "changed_files": ["docs/CLI_COMMANDS.yaml"],
                }
            }
        )
        assert adapter.calls == 0

        # The folder itself: direct evidence, so the window does not apply.
        await AgentSkillsService.handle_post_action_event(
            {
                "meta": {
                    "environment_id": str(env.id),
                    "changed_files": ["docs/CLI_COMMANDS.yaml", SKILLS_DIR_PATH],
                }
            }
        )
        assert adapter.calls == 1

    @pytest.mark.anyio
    async def test_a_malformed_event_never_escapes_the_handler(
        self, stub_adapter, monkeypatch
    ):
        # The handler runs as a fire-and-forget task on the event bus: raising
        # here would surface as an unhandled task exception on an unrelated
        # stream, so every failure is swallowed.
        env = _environment()
        stub_adapter(_FakeAdapter(_payload([_reported()])))
        self._patch_session(monkeypatch, _FakeSession(env))

        await AgentSkillsService.handle_post_action_event(
            {"meta": {"environment_id": "not-a-uuid"}}
        )
        await AgentSkillsService.handle_post_action_event({"meta": "not-a-dict"})


class TestSkillVersionInTheCache:
    """The frontmatter ``version`` travelling container → cache → row."""

    def test_a_reported_version_is_normalised_and_read_back(self):
        rows = AgentSkillsService._normalise_entries(
            [_reported(version="1.0.0")]
        )
        assert rows[0]["version"] == "1.0.0"

    def test_an_unquoted_numeric_version_survives_as_a_string(self):
        # The container's parser has already coerced `version: 1.0` to a float;
        # caching it as one would put a number where the wire says string.
        rows = AgentSkillsService._normalise_entries([_reported(version=1.0)])
        assert rows[0]["version"] == "1.0"

    def test_a_container_built_before_versions_caches_none(self):
        # A pre-feature env-core reports rows with no `version` key at all. The
        # index must still normalise rather than raise, or one old container
        # blanks the whole skills list.
        entry = _reported()
        del entry["version"]
        rows = AgentSkillsService._normalise_entries([entry])
        assert rows[0]["version"] is None


class TestLocalVersionBackfill:
    """The host filling in a version an old container cannot report.

    ``app/core/`` is copied out of the template at environment *creation*, so
    an environment made before skills carried a version reports rows with no
    ``version`` key however many times its author edits ``SKILL.md``. The host
    reads the same file the publish path reads.
    """

    @staticmethod
    def _workspace(monkeypatch, tmp_path, env) -> "Path":
        from pathlib import Path

        from app.core.config import settings
        from app.services.environments.workspace_classification import (
            WORKSPACE_ROOT_REL,
        )

        monkeypatch.setattr(settings, "ENV_INSTANCES_DIR", str(tmp_path))
        skills = Path(tmp_path) / str(env.id) / WORKSPACE_ROOT_REL / "skills"
        skills.mkdir(parents=True)
        return skills

    def test_a_missing_version_is_read_off_the_workspace(
        self, monkeypatch, tmp_path
    ):
        env = _environment()
        skills = self._workspace(monkeypatch, tmp_path, env)
        (skills / "pdf-report").mkdir()
        (skills / "pdf-report" / "SKILL.md").write_text(
            "---\nname: pdf-report\nversion: 2.1.0\ndescription: d\n---\nbody\n",
            encoding="utf-8",
        )

        rows = AgentSkillsService._normalise_entries([_reported()])
        AgentSkillsService._backfill_local_versions(env, rows)

        assert rows[0]["version"] == "2.1.0"

    def test_a_version_the_container_reported_is_not_overwritten(
        self, monkeypatch, tmp_path
    ):
        # A rebuilt container is the thing that actually loaded the skill; the
        # host must not second-guess it.
        env = _environment()
        skills = self._workspace(monkeypatch, tmp_path, env)
        (skills / "pdf-report").mkdir()
        (skills / "pdf-report" / "SKILL.md").write_text(
            "---\nname: pdf-report\nversion: 9.9.9\ndescription: d\n---\nbody\n",
            encoding="utf-8",
        )

        rows = AgentSkillsService._normalise_entries([_reported(version="1.0.0")])
        AgentSkillsService._backfill_local_versions(env, rows)

        assert rows[0]["version"] == "1.0.0"

    def test_plugin_skills_are_left_alone(self, monkeypatch, tmp_path):
        # A plugin's skill does not live under the workspace's `skills/`, and
        # its version is its publisher's business.
        env = _environment()
        self._workspace(monkeypatch, tmp_path, env)
        rows = AgentSkillsService._normalise_entries(
            [_reported(source="plugin", plugin_ref="official/pdf")]
        )
        AgentSkillsService._backfill_local_versions(env, rows)
        assert rows[0]["version"] is None

    def test_a_workspace_that_is_not_on_disk_changes_nothing(
        self, monkeypatch, tmp_path
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "ENV_INSTANCES_DIR", str(tmp_path))
        rows = AgentSkillsService._normalise_entries([_reported()])
        AgentSkillsService._backfill_local_versions(_environment(), rows)
        assert rows[0]["version"] is None

    def test_a_skill_with_no_version_in_its_header_stays_unversioned(
        self, monkeypatch, tmp_path
    ):
        env = _environment()
        skills = self._workspace(monkeypatch, tmp_path, env)
        (skills / "pdf-report").mkdir()
        (skills / "pdf-report" / "SKILL.md").write_text(
            "---\nname: pdf-report\ndescription: d\n---\nbody\n", encoding="utf-8"
        )
        rows = AgentSkillsService._normalise_entries([_reported()])
        AgentSkillsService._backfill_local_versions(env, rows)
        assert rows[0]["version"] is None

    def test_a_name_that_is_not_a_skill_name_is_never_joined_into_a_path(
        self, monkeypatch, tmp_path
    ):
        # The index is written by a process inside the agent's own container,
        # so the name is guarded as a path segment, not merely validated.
        env = _environment()
        self._workspace(monkeypatch, tmp_path, env)
        rows = AgentSkillsService._normalise_entries(
            [_reported(name="../../etc")]
        )
        AgentSkillsService._backfill_local_versions(env, rows)
        assert rows[0]["version"] is None
