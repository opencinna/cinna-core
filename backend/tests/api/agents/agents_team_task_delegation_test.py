"""
Integration test: agentic team task delegation full lifecycle.

Exercises the complete flow of a 2-agent team collaborating on a task:
- User creates a team with lead and worker agents, wired with a connection
- User creates a team-scoped task assigned to the lead agent
- User executes the task — lead agent's SDK calls create_subtask during stream
- Worker agent's subtask auto-executes, posts comments via MCP tools during stream
- Session completion event handlers automatically sync task statuses
- Full task tree verified with all comments, statuses, and hierarchy

Both agents use ScriptedAgentEnvConnector which simulates real agent-env behavior:
the SDK processes a message, calls MCP tools (HTTP requests back to the backend)
mid-stream, then completes. Status transitions are driven by session lifecycle
events — no explicit agent_update_status workarounds.

This test validates the interaction between:
  Backend API ←→ Agent Environment (stubbed with scripted tool calls) ←→ MCP Tools
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import (
    StubAgentEnvConnector,
    ScriptedAgentEnvConnector,
)
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.agentic_team import (
    create_team,
    create_node,
    create_connection,
    update_team,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.input_task import (
    create_task,
    get_task,
    get_task_detail,
    get_task_by_code,
    get_task_tree_by_code,
    list_comments,
    execute_task,
    get_task_sessions,
    agent_create_subtask,
    agent_add_comment,
    agent_get_task_details,
)


_BASE = f"{settings.API_V1_STR}"


def test_agentic_team_full_delegation_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Full agentic team task delegation with session-driven completion:
      1.  Create two agents (Lead, Worker) and a team "HR" with connection
      2.  Create team-scoped task — auto-assigned to lead
      3.  Execute task — lead SDK calls create_subtask mid-stream
      4.  Worker auto-executes with scripted add_comment MCP calls
      5.  Session completion automatically transitions subtask → completed
      6.  Parent task stays in_progress until subtask completes, then → completed
      7.  Lead reads subtask details, posts summary on parent
      8.  Verify full task tree, comments, status history
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agents and team ──────────────────────────────────
    lead_agent = create_agent_via_api(client, headers, name="HR Lead Agent")
    worker_agent = create_agent_via_api(client, headers, name="Recruiting Agent")
    drain_tasks()

    lead_agent = get_agent(client, headers, lead_agent["id"])
    worker_agent = get_agent(client, headers, worker_agent["id"])
    lead_agent_id = lead_agent["id"]
    worker_agent_id = worker_agent["id"]

    team = create_team(client, headers, name="HR Team")
    team_id = team["id"]
    team = update_team(client, headers, team_id, task_prefix="HR")

    lead_node = create_node(client, headers, team_id, lead_agent_id, is_lead=True)
    worker_node = create_node(client, headers, team_id, worker_agent_id)
    worker_node_name = worker_node["name"]

    create_connection(
        client, headers, team_id,
        source_node_id=lead_node["id"],
        target_node_id=worker_node["id"],
        connection_prompt="Delegate recruiting-related subtasks to this agent.",
        enabled=True,
    )

    # ── Phase 2: Create team-scoped task ─────────────────────────────────
    parent_task = create_task(
        client, headers,
        original_message="Hire a senior backend engineer. "
                         "Need 3 qualified candidates within 2 weeks.",
        title="Hire Senior Backend Engineer",
        priority="high",
        team_id=team_id,
    )
    parent_task_id = parent_task["id"]
    parent_short_code = parent_task["short_code"]

    assert parent_short_code.startswith("HR-")
    assert parent_task["status"] == "new"
    assert parent_task["selected_agent_id"] == lead_agent_id
    assert parent_task["assigned_node_id"] == lead_node["id"]

    # ── Phase 3: Execute — lead SDK creates subtask mid-stream ───────────
    # execute_task returns immediately (only schedules process_pending_messages).
    # We build the lead stub AFTER getting the session_id so the create_subtask
    # tool call can include source_session_id (needed for feedback delivery
    # when the subtask completes — this is how the real MCP tool works).
    with patch("app.services.sessions.message_service.agent_env_connector", StubAgentEnvConnector(response_text="placeholder")):
        exec_result = execute_task(client, headers, parent_task_id)

    assert exec_result["success"] is True
    lead_session_id = str(exec_result["session_id"])

    # Now build the real lead stub with session_id available for tool call
    lead_stub = ScriptedAgentEnvConnector(
        client=client,
        auth_headers=headers,
        steps=[
            {
                "type": "assistant",
                "content": "I'll delegate candidate sourcing to the recruiting team.",
            },
            {
                "type": "tool_call",
                "endpoint": f"{_BASE}/agent/tasks/{parent_task_id}/subtask",
                "method": "POST",
                "json": {
                    "title": "Source Senior Backend Candidates",
                    "description": "Find 3 qualified senior backend engineers "
                                   "with Python/FastAPI experience.",
                    "assigned_to": worker_node_name,
                    "priority": "high",
                    "source_session_id": lead_session_id,
                },
                "tool_name": "mcp__agent_task__create_subtask",
            },
            {
                "type": "assistant",
                "content": "Subtask delegated. Waiting for results.",
            },
        ],
    )

    # Drain runs the lead's stream (process_pending_messages). The create_subtask
    # tool call fires mid-stream (with source_session_id), creating the subtask
    # and scheduling _auto_execute_subtask. The lead's "done" triggers
    # STREAM_COMPLETED → sync finds subtask still pending → parent stays in_progress.
    # The auto-execute runs (lead_stub fallback), worker session completes →
    # subtask completed → feedback sent to lead session → re-stream → parent
    # re-synced → parent completed.
    with patch("app.services.sessions.message_service.agent_env_connector", lead_stub):
        drain_tasks()

    # Verify the create_subtask tool call succeeded during the lead's stream
    assert len(lead_stub.tool_results) == 1
    subtask_tool_result = lead_stub.tool_results[0]
    assert subtask_tool_result["status_code"] == 200
    subtask_short_code = subtask_tool_result["body"]["task"]
    assert subtask_short_code.startswith("HR-")
    assert subtask_tool_result["body"]["parent_task"] == parent_short_code

    # ── Phase 4: Verify subtask properties and session-driven status ─────
    subtask_by_code = get_task_by_code(client, headers, subtask_short_code)
    subtask_id = subtask_by_code["id"]

    assert subtask_by_code["parent_task_id"] == parent_task_id
    assert subtask_by_code["team_id"] == team_id
    assert subtask_by_code["assigned_node_id"] == worker_node["id"]
    assert subtask_by_code["selected_agent_id"] == worker_agent_id

    # Worker session auto-executed and completed → event handlers synced
    # subtask to completed. No explicit agent_update_status needed.
    assert subtask_by_code["status"] == "completed"

    # Parent: all sessions completed + all subtasks completed → completed
    parent_refreshed = get_task(client, headers, parent_task_id)
    assert parent_refreshed["status"] == "completed"

    # Session was created for the lead
    task_sessions = get_task_sessions(client, headers, parent_task_id)
    assert any(s["id"] == lead_session_id for s in task_sessions)

    # ── Phase 5: Verify comments ─────────────────────────────────────────
    # System comment on parent confirms delegation
    parent_comments = list_comments(client, headers, parent_task_id)
    delegation_comments = [
        c for c in parent_comments["data"]
        if c["comment_type"] == "system" and subtask_short_code in c["content"]
    ]
    assert len(delegation_comments) >= 1

    # ── Phase 6: Lead reads subtask details ──────────────────────────────
    subtask_details = agent_get_task_details(client, headers, task_id=subtask_id)
    assert subtask_details["task"] == subtask_short_code
    assert subtask_details["status"] == "completed"

    # ── Phase 7: Lead posts summary on parent ────────────────────────────
    agent_add_comment(
        client, headers,
        task_id=parent_task_id,
        content="Recruiting completed. 3 qualified candidates identified:\n"
                "1. Alice Chen (recommended)\n"
                "2. Bob Rivera\n"
                "3. Carol Wu",
        comment_type="result",
    )

    # ── Phase 8: Verify full task tree and detail ────────────────────────
    tree = get_task_tree_by_code(client, headers, parent_short_code)
    assert tree["short_code"] == parent_short_code
    assert len(tree["subtasks"]) == 1
    assert tree["subtasks"][0]["short_code"] == subtask_short_code

    parent_detail = get_task_detail(client, headers, parent_task_id)
    assert parent_detail["status"] == "completed"
    assert parent_detail["team_id"] == team_id
    assert parent_detail["subtask_count"] == 1
    assert parent_detail["subtask_completed_count"] == 1

    # Result comments on parent
    result_comments = [
        c for c in parent_detail["comments"] if c["comment_type"] == "result"
    ]
    assert len(result_comments) >= 1
    assert any("Alice Chen" in c["content"] for c in result_comments)

    # Status history includes in_progress transition (from link_session).
    # The session-driven "completed" transition uses update_status (internal sync)
    # which does not create history entries — only update_task_status does.
    status_history = parent_detail["status_history"]
    history_statuses = [h["to_status"] for h in status_history]
    assert "in_progress" in history_statuses


def test_agentic_team_scripted_worker_comments_during_stream(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Worker agent posts comments via MCP tools during its session stream:
      1. Create team, parent task, delegate subtask
      2. Worker's auto-execute uses ScriptedAgentEnvConnector with add_comment steps
      3. After drain, comments exist on the subtask from mid-stream tool calls
      4. Session completion automatically transitions subtask → completed
    """
    headers = superuser_token_headers

    lead = create_agent_via_api(client, headers, name="Script Lead")
    worker = create_agent_via_api(client, headers, name="Script Worker")
    drain_tasks()

    team = create_team(client, headers, name="Script Team")
    team_id = team["id"]
    team = update_team(client, headers, team_id, task_prefix="SCR")

    lead_node = create_node(client, headers, team_id, lead["id"], is_lead=True)
    worker_node = create_node(client, headers, team_id, worker["id"])
    create_connection(
        client, headers, team_id,
        source_node_id=lead_node["id"],
        target_node_id=worker_node["id"],
        enabled=True,
    )

    parent = create_task(
        client, headers,
        original_message="Research market trends",
        team_id=team_id,
        selected_agent_id=lead["id"],
    )
    parent_id = parent["id"]

    # Execute lead's task
    lead_stub = StubAgentEnvConnector(response_text="Starting coordination.")
    with patch("app.services.sessions.message_service.agent_env_connector", lead_stub):
        exec_result = execute_task(client, headers, parent_id)
        drain_tasks()

    lead_session_id = str(exec_result["session_id"])

    # Create subtask — schedules auto-execute as background task
    subtask_result = agent_create_subtask(
        client, headers,
        task_id=parent_id,
        title="Analyze competitor data",
        description="Review Q1 reports from top 5 competitors",
        assigned_to=worker_node["name"],
        source_session_id=lead_session_id,
    )
    subtask_code = subtask_result["task"]
    subtask_data = get_task_by_code(client, headers, subtask_code)
    subtask_id = subtask_data["id"]

    # Worker's auto-execute uses scripted MCP tool calls
    worker_stub = ScriptedAgentEnvConnector(
        client=client,
        auth_headers=headers,
        steps=[
            {"type": "assistant", "content": "Analyzing competitor reports..."},
            {
                "type": "tool_call",
                "endpoint": f"{_BASE}/agent/tasks/{subtask_id}/comment",
                "method": "POST",
                "json": {
                    "content": "Progress: reviewed 3 of 5 competitor reports.",
                    "comment_type": "message",
                },
                "tool_name": "mcp__agent_task__add_comment",
            },
            {
                "type": "tool_call",
                "endpoint": f"{_BASE}/agent/tasks/{subtask_id}/comment",
                "method": "POST",
                "json": {
                    "content": "Analysis complete. Key findings:\n"
                               "- Competitor A: 30% market share growth\n"
                               "- Competitor B: New product launch Q2\n"
                               "- Competitor C: Price reduction strategy",
                    "comment_type": "result",
                },
                "tool_name": "mcp__agent_task__add_comment",
            },
            {"type": "assistant", "content": "All reports analyzed. Results posted."},
        ],
    )

    with patch("app.services.sessions.message_service.agent_env_connector", worker_stub):
        drain_tasks()

    # Verify MCP tool calls succeeded during stream
    assert len(worker_stub.tool_results) >= 2
    assert worker_stub.tool_results[0]["status_code"] == 200
    assert worker_stub.tool_results[1]["status_code"] == 200

    # Comments exist on the subtask from mid-stream tool calls
    subtask_comments = list_comments(client, headers, subtask_id)
    comment_contents = [c["content"] for c in subtask_comments["data"]]
    assert any("reviewed 3 of 5" in c for c in comment_contents)
    assert any("Competitor A" in c for c in comment_contents)

    result_comments = [
        c for c in subtask_comments["data"] if c["comment_type"] == "result"
    ]
    assert len(result_comments) >= 1

    # Session-driven completion — subtask completed automatically
    subtask_final = get_task(client, headers, subtask_id)
    assert subtask_final["status"] == "completed"

    # Status history includes in_progress from link_session
    subtask_detail = get_task_detail(client, headers, subtask_id)
    history_statuses = [h["to_status"] for h in subtask_detail["status_history"]]
    assert "in_progress" in history_statuses


def test_agentic_team_delegation_topology_enforcement(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Delegation topology enforcement:
      1. Create 3 agents: Lead, Worker A, Worker B
      2. Create team with connection Lead→A only (no Lead→B)
      3. Create parent task with lead agent
      4. Agent delegates to Worker A — succeeds
      5. Agent delegates to Worker B — fails (no connection)
      6. Agent delegates to non-existent team member — fails
    """
    headers = superuser_token_headers

    lead = create_agent_via_api(client, headers, name="Topo Lead")
    worker_a = create_agent_via_api(client, headers, name="Topo Worker A")
    worker_b = create_agent_via_api(client, headers, name="Topo Worker B")
    drain_tasks()

    team = create_team(client, headers, name="Topology Team")
    team_id = team["id"]

    lead_node = create_node(client, headers, team_id, lead["id"], is_lead=True)
    node_a = create_node(client, headers, team_id, worker_a["id"])
    node_b = create_node(client, headers, team_id, worker_b["id"])

    create_connection(
        client, headers, team_id,
        source_node_id=lead_node["id"],
        target_node_id=node_a["id"],
        enabled=True,
    )

    parent = create_task(
        client, headers,
        original_message="Topology enforcement test task",
        team_id=team_id,
        selected_agent_id=lead["id"],
    )
    parent_id = parent["id"]

    # ── Delegate to Worker A — should succeed ────────────────────────────
    stub_a = StubAgentEnvConnector(response_text="On it")
    with patch("app.services.sessions.message_service.agent_env_connector", stub_a):
        result_a = agent_create_subtask(
            client, headers,
            task_id=parent_id,
            title="Task for Worker A",
            assigned_to=node_a["name"],
        )
        drain_tasks()
    assert result_a["success"] is True

    # ── Delegate to Worker B — should fail (no connection) ───────────────
    r = client.post(
        f"{_BASE}/agent/tasks/{parent_id}/subtask",
        headers=headers,
        json={"title": "Task for Worker B", "assigned_to": node_b["name"]},
    )
    assert r.status_code == 400
    assert "connection" in r.json()["detail"].lower() or "topology" in r.json()["detail"].lower()

    # ── Delegate to non-existent member — should fail ────────────────────
    r = client.post(
        f"{_BASE}/agent/tasks/{parent_id}/subtask",
        headers=headers,
        json={"title": "Task for Ghost Agent", "assigned_to": "Nonexistent Agent"},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"].lower()


def test_agentic_team_task_short_code_prefix(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Team task_prefix controls short-code generation:
      1. Create team with prefix "ENG"
      2. Create task scoped to team — short code starts with "ENG-"
      3. Create task without team — short code starts with "TASK-"
      4. Agent creates subtask in team — subtask also uses team prefix
    """
    headers = superuser_token_headers

    agent = create_agent_via_api(client, headers, name="Prefix Test Agent")
    drain_tasks()

    team = create_team(client, headers, name="Engineering Team")
    team_id = team["id"]
    team = update_team(client, headers, team_id, task_prefix="ENG")

    create_node(client, headers, team_id, agent["id"], is_lead=True)

    team_task = create_task(client, headers, original_message="Engineering task", team_id=team_id)
    assert team_task["short_code"].startswith("ENG-")

    standalone_task = create_task(client, headers, original_message="Standalone task")
    assert standalone_task["short_code"].startswith("TASK-")

    subtask_result = agent_create_subtask(
        client, headers, task_id=team_task["id"], title="Sub-engineering task",
    )
    drain_tasks()
    assert subtask_result["success"] is True
    assert subtask_result["task"].startswith("ENG-")


def test_agentic_team_current_session_endpoints(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Agent "current" endpoints resolve task from session:
      1. Create team, parent task, execute it (creates session)
      2. POST /agent/tasks/current/subtask with source_session_id
      3. POST /agent/tasks/current/comment with source_session_id
      4. GET /agent/tasks/current/details with source_session_id
      5. All resolve correctly to the parent task
    """
    headers = superuser_token_headers

    lead = create_agent_via_api(client, headers, name="Current EP Lead")
    worker = create_agent_via_api(client, headers, name="Current EP Worker")
    drain_tasks()

    team = create_team(client, headers, name="Current EP Team")
    team_id = team["id"]

    lead_node = create_node(client, headers, team_id, lead["id"], is_lead=True)
    worker_node = create_node(client, headers, team_id, worker["id"])
    create_connection(
        client, headers, team_id,
        source_node_id=lead_node["id"],
        target_node_id=worker_node["id"],
        enabled=True,
    )

    parent = create_task(
        client, headers,
        original_message="Current endpoint test task",
        team_id=team_id,
        selected_agent_id=lead["id"],
    )
    parent_id = parent["id"]

    stub_lead = StubAgentEnvConnector(response_text="Acknowledged")
    with patch("app.services.sessions.message_service.agent_env_connector", stub_lead):
        exec_result = execute_task(client, headers, parent_id)
        drain_tasks()

    session_id = str(exec_result["session_id"])

    # ── POST /agent/tasks/current/subtask ────────────────────────────────
    from tests.utils.input_task import agent_create_subtask_current
    stub_worker = StubAgentEnvConnector(response_text="Working on it")
    with patch("app.services.sessions.message_service.agent_env_connector", stub_worker):
        sub_result = agent_create_subtask_current(
            client, headers,
            title="Subtask via current endpoint",
            source_session_id=session_id,
            assigned_to=worker_node["name"],
        )
        drain_tasks()

    assert sub_result["success"] is True
    assert sub_result["parent_task"] == parent["short_code"]

    # ── POST /agent/tasks/current/comment ────────────────────────────────
    from tests.utils.input_task import agent_add_comment_current
    comment = agent_add_comment_current(
        client, headers,
        content="Progress update from current endpoint",
        source_session_id=session_id,
    )
    # Response is AgentCommentResponse: comment_id, task (short_code), attachments_count
    assert comment["task"] == parent["short_code"]

    # ── GET /agent/tasks/current/details ─────────────────────────────────
    from tests.utils.input_task import agent_get_task_details_current
    details = agent_get_task_details_current(client, headers, source_session_id=session_id)
    assert details["task"] == parent["short_code"]
    recent_contents = [c["content"] for c in details["recent_comments"]]
    assert any("Progress update" in c for c in recent_contents)
