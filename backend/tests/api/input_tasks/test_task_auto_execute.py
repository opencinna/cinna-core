"""
Tests for the auto-execute on task creation feature.

When a task is created with ``auto_execute=True`` and a ``selected_agent_id``,
the POST /tasks/ endpoint schedules ``InputTaskService._auto_execute_task`` as a
background job immediately after returning the task.  That job opens its own DB
session, calls ``InputTaskService.execute_task``, and streams the task description
to the agent — which ultimately completes the session and transitions the task to
``completed``.

Scenarios:
  1. auto_execute=True + valid selected_agent_id
       - Task created (status=new), auto_execute flag stored
       - Background job is scheduled (captured by BackgroundTaskCollector)
       - Draining tasks with a streaming stub runs the session and completes the task
       - At least one session is linked to the task after drain
       - Task status transitions to completed (session-driven, not manual)

  2. auto_execute=True + NO selected_agent_id
       - Task created normally
       - No background execution job is scheduled (nothing to drain)
       - Task remains in 'new' status

  3. auto_execute=False + valid selected_agent_id
       - Task created normally
       - No background execution job is scheduled
       - Task remains in 'new' status

  4. auto_execute not specified (default)
       - Defaults to False
       - Task created with auto_execute=False stored
       - No background execution triggered
       - Task remains in 'new' status

  5. auto_execute=True + invalid/non-existent selected_agent_id
       - 400 or 404 from verify_agent_access — task is NOT created

  6. Creating multiple auto_execute tasks sequentially
       - Each task gets its own background job
       - Both complete independently after drain

  7. auto_execute=True + file_ids (regression: files were never delivered)
       - Files uploaded via POST /files/upload, task created with their ids
       - Auto-execute job calls InputTaskService.execute_task with
         file_ids == the uploaded file UUIDs (as UUID objects)

  8. auto_execute=True without file_ids
       - Auto-execute job calls execute_task with file_ids=None
"""
import io
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.input_task import (
    create_task,
    get_task,
    get_task_sessions,
)

_BASE = f"{settings.API_V1_STR}/tasks"
_AGENT_ENV_PATCH = "app.services.sessions.message_service.agent_env_connector"
_EXECUTE_TASK_PATCH = (
    "app.services.tasks.input_task_service.InputTaskService.execute_task"
)


def test_auto_execute_true_with_agent_triggers_execution(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    auto_execute=True with a valid selected_agent_id triggers background execution:
      1. Create agent
      2. Create task with auto_execute=True and selected_agent_id
      3. Task is returned immediately with status 'new' and auto_execute=True
      4. Drain tasks with a streaming stub — auto-execute job runs
      5. Task has at least one session linked (execute_task was called)
      6. Task status transitions to 'completed' (session-driven)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ────────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Auto Execute Agent")
    drain_tasks()  # drain agent creation background tasks
    agent_id = agent["id"]

    # ── Phase 2: Create task with auto_execute=True ──────────────────────────
    stub = StubAgentEnvConnector(response_text="Task completed by auto-execute agent.")
    with patch(_AGENT_ENV_PATCH, stub):
        task = create_task(
            client, headers,
            original_message="Summarise the quarterly report.",
            selected_agent_id=agent_id,
            auto_execute=True,
        )
        # ── Phase 3: Immediate response ──────────────────────────────────────
        assert task["status"] == "new"
        assert task["auto_execute"] is True
        assert task["selected_agent_id"] == agent_id
        task_id = task["id"]

        # ── Phase 4: Drain — auto-execute job runs the session ───────────────
        drain_tasks()

    # ── Phase 5: Session was created for the task ────────────────────────────
    task_sessions = get_task_sessions(client, headers, task_id)
    assert len(task_sessions) >= 1, (
        "Expected at least one session after auto-execute drain, got none"
    )

    # ── Phase 6: Task completed via session lifecycle events ─────────────────
    refreshed = get_task(client, headers, task_id)
    assert refreshed["status"] == "completed", (
        f"Expected task status 'completed' after auto-execute, got '{refreshed['status']}'"
    )


def test_auto_execute_true_without_agent_does_not_trigger_execution(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    auto_execute=True with NO selected_agent_id creates the task but does NOT
    trigger background execution:
      1. Create task with auto_execute=True and no selected_agent_id
      2. Task is created successfully
      3. No background job is scheduled — no sessions created after drain
      4. Task remains in 'new' status
    """
    headers = superuser_token_headers

    # ── Phase 1: Create task (no agent assigned) ─────────────────────────────
    task = create_task(
        client, headers,
        original_message="Auto execute without agent.",
        auto_execute=True,
        # selected_agent_id intentionally omitted
    )
    task_id = task["id"]

    # ── Phase 2: Task created with correct fields ────────────────────────────
    assert task["status"] == "new"
    assert task["auto_execute"] is True
    assert task["selected_agent_id"] is None

    # ── Phase 3: Drain — no execution job to run ─────────────────────────────
    drain_tasks()

    # ── Phase 4: No sessions linked — execute_task was never called ──────────
    task_sessions = get_task_sessions(client, headers, task_id)
    assert len(task_sessions) == 0, (
        f"Expected no sessions for task with auto_execute=True but no agent, "
        f"got {len(task_sessions)}"
    )

    # Task status stays 'new'
    refreshed = get_task(client, headers, task_id)
    assert refreshed["status"] == "new"


def test_auto_execute_false_with_agent_does_not_trigger_execution(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    auto_execute=False with a valid selected_agent_id creates the task but does NOT
    trigger background execution:
      1. Create agent
      2. Create task with auto_execute=False and selected_agent_id
      3. Task is created successfully
      4. No background execution job is scheduled
      5. Task remains in 'new' status
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ────────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="No Auto Execute Agent")
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 2: Create task with auto_execute=False ─────────────────────────
    task = create_task(
        client, headers,
        original_message="This task should not auto-execute.",
        selected_agent_id=agent_id,
        auto_execute=False,
    )
    task_id = task["id"]

    # ── Phase 3: Task created with correct fields ────────────────────────────
    assert task["status"] == "new"
    assert task["auto_execute"] is False
    assert task["selected_agent_id"] == agent_id

    # ── Phase 4: Drain — no auto-execute job exists ──────────────────────────
    drain_tasks()

    # ── Phase 5: No sessions linked ──────────────────────────────────────────
    task_sessions = get_task_sessions(client, headers, task_id)
    assert len(task_sessions) == 0, (
        f"Expected no sessions for task with auto_execute=False, "
        f"got {len(task_sessions)}"
    )

    refreshed = get_task(client, headers, task_id)
    assert refreshed["status"] == "new"


def test_auto_execute_default_does_not_trigger_execution(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When auto_execute is not provided in the request, it defaults to False and
    no background execution is triggered:
      1. Create agent
      2. Create task with selected_agent_id but WITHOUT specifying auto_execute
      3. Response shows auto_execute defaults to False
      4. No background job scheduled — no sessions, task stays 'new'
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ────────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Default Auto Execute Agent")
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 2: Create task without auto_execute field ──────────────────────
    # Use client.post directly to confirm the default is False, not the helper
    r = client.post(
        _BASE + "/",
        headers=headers,
        json={
            "original_message": "Task with default auto_execute.",
            "selected_agent_id": agent_id,
        },
    )
    assert r.status_code == 200, f"Task creation failed: {r.text}"
    task = r.json()
    task_id = task["id"]

    # ── Phase 3: auto_execute defaults to False ──────────────────────────────
    assert task["auto_execute"] is False
    assert task["selected_agent_id"] == agent_id
    assert task["status"] == "new"

    # ── Phase 4: Drain — no auto-execute job ─────────────────────────────────
    drain_tasks()

    # ── Phase 5: No sessions created ─────────────────────────────────────────
    task_sessions = get_task_sessions(client, headers, task_id)
    assert len(task_sessions) == 0

    refreshed = get_task(client, headers, task_id)
    assert refreshed["status"] == "new"


def test_auto_execute_invalid_agent_id_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    auto_execute=True with a non-existent selected_agent_id is rejected before
    the task is created:
      1. POST /tasks/ with auto_execute=True and a random UUID as selected_agent_id
      2. verify_agent_access raises AgentNotFoundError → 404 response
      3. No task is created
    """
    import uuid
    headers = superuser_token_headers

    fake_agent_id = str(uuid.uuid4())
    r = client.post(
        _BASE + "/",
        headers=headers,
        json={
            "original_message": "This should not be created.",
            "selected_agent_id": fake_agent_id,
            "auto_execute": True,
        },
    )
    # verify_agent_access raises AgentNotFoundError (404) or PermissionDeniedError (400)
    assert r.status_code in (400, 404), (
        f"Expected 400/404 for invalid agent_id, got {r.status_code}: {r.text}"
    )


def test_auto_execute_two_tasks_both_complete(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Two tasks with auto_execute=True are created sequentially; both complete:
      1. Create agent
      2. Create first task with auto_execute=True
      3. Create second task with auto_execute=True
      4. Drain tasks with streaming stub — both auto-execute jobs run
      5. Both tasks have sessions and reach 'completed' status
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ────────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Multi Auto Execute Agent")
    drain_tasks()
    agent_id = agent["id"]

    stub = StubAgentEnvConnector(response_text="Task done.")
    with patch(_AGENT_ENV_PATCH, stub):
        # ── Phase 2: Create first task ───────────────────────────────────────
        task1 = create_task(
            client, headers,
            original_message="First auto-execute task.",
            selected_agent_id=agent_id,
            auto_execute=True,
        )
        task1_id = task1["id"]
        assert task1["auto_execute"] is True

        # ── Phase 3: Create second task ──────────────────────────────────────
        task2 = create_task(
            client, headers,
            original_message="Second auto-execute task.",
            selected_agent_id=agent_id,
            auto_execute=True,
        )
        task2_id = task2["id"]
        assert task2["auto_execute"] is True

        # ── Phase 4: Drain — both jobs run ───────────────────────────────────
        drain_tasks()

    # ── Phase 5: Both tasks completed ────────────────────────────────────────
    sessions1 = get_task_sessions(client, headers, task1_id)
    assert len(sessions1) >= 1, "Task 1 expected a session after auto-execute"

    sessions2 = get_task_sessions(client, headers, task2_id)
    assert len(sessions2) >= 1, "Task 2 expected a session after auto-execute"

    refreshed1 = get_task(client, headers, task1_id)
    assert refreshed1["status"] == "completed", (
        f"Task 1 expected 'completed', got '{refreshed1['status']}'"
    )

    refreshed2 = get_task(client, headers, task2_id)
    assert refreshed2["status"] == "completed", (
        f"Task 2 expected 'completed', got '{refreshed2['status']}'"
    )


def test_auto_execute_passes_uploaded_file_ids_to_execute_task(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path,
) -> None:
    """
    Regression: files attached at creation reach the agent on auto-execute:
      1. Create agent
      2. Upload two files via POST /files/upload
      3. Create task with auto_execute=True, selected_agent_id and file_ids
      4. Drain with execute_task mocked
      5. execute_task called once with file_ids == uploaded UUIDs (as UUID objects)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ────────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Auto Execute Files Agent")
    drain_tasks()
    agent_id = agent["id"]

    with patch.object(settings, "UPLOAD_BASE_PATH", str(tmp_path / "uploads")):
        # ── Phase 2: Upload files ────────────────────────────────────────────
        uploaded_ids: list[str] = []
        for name in ("first.txt", "second.txt"):
            r = client.post(
                f"{settings.API_V1_STR}/files/upload",
                headers=headers,
                files={"file": (name, io.BytesIO(f"content of {name}".encode()), "text/plain")},
            )
            assert r.status_code == 200, f"File upload failed: {r.text}"
            uploaded_ids.append(r.json()["id"])

        # ── Phase 3: Create task with file_ids + auto_execute ────────────────
        with patch(_EXECUTE_TASK_PATCH, new_callable=AsyncMock) as execute_mock:
            execute_mock.return_value = (True, None, None)
            r = client.post(
                _BASE + "/",
                headers=headers,
                json={
                    "original_message": "Summarise the attached files.",
                    "title": "Auto execute with files",
                    "selected_agent_id": agent_id,
                    "auto_execute": True,
                    "file_ids": uploaded_ids,
                },
            )
            assert r.status_code == 200, f"Task creation failed: {r.text}"
            assert r.json()["auto_execute"] is True

            # ── Phase 4: Drain — auto-execute job runs ───────────────────────
            drain_tasks()

        # ── Phase 5: execute_task received the uploaded files ────────────────
        execute_mock.assert_called_once()
        passed = execute_mock.call_args.kwargs["file_ids"]
        assert passed is not None
        assert all(isinstance(fid, uuid.UUID) for fid in passed)
        assert set(passed) == {uuid.UUID(fid) for fid in uploaded_ids}
        assert len(passed) == len(uploaded_ids)


def test_auto_execute_without_files_passes_none_file_ids(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Auto-execute of a task with no files passes file_ids=None:
      1. Create agent
      2. Create task with auto_execute=True and no file_ids
      3. Drain with execute_task mocked
      4. execute_task called once with file_ids=None
    """
    headers = superuser_token_headers

    agent = create_agent_via_api(client, headers, name="Auto Execute No Files Agent")
    drain_tasks()

    with patch(_EXECUTE_TASK_PATCH, new_callable=AsyncMock) as execute_mock:
        execute_mock.return_value = (True, None, None)
        task = create_task(
            client, headers,
            original_message="No files here.",
            title="Auto execute without files",
            selected_agent_id=agent["id"],
            auto_execute=True,
        )
        assert task["auto_execute"] is True
        drain_tasks()

    execute_mock.assert_called_once()
    assert execute_mock.call_args.kwargs["file_ids"] is None
