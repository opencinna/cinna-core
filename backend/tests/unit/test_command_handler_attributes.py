"""
Unit test for the ``include_in_llm_context`` class attribute on the non-LLM
command handlers.

Pure attribute introspection — imports the handler classes and asserts their
opt-in/opt-out flags, no I/O. The handler-registration and bridge behavior is
covered in ``tests/api/agents/commands/agents_non_llm_bridge_test.py``.
"""


def test_include_in_llm_context_attributes_are_set_correctly() -> None:
    """Verify handler class attributes match the plan spec."""
    from app.services.agents.commands.files_command import (
        FilesCommandHandler,
        FilesAllCommandHandler,
    )
    from app.services.agents.commands.agent_status_command import AgentStatusCommandHandler
    from app.services.agents.commands.webapp_command import WebappCommandHandler
    from app.services.agents.commands.session_recover_command import SessionRecoverCommandHandler
    from app.services.agents.commands.session_reset_command import SessionResetCommandHandler
    from app.services.agents.commands.rebuild_env_command import RebuildEnvCommandHandler
    from app.services.agents.commands.run_command import RunCommandHandler

    # Opted-in (default True)
    assert FilesCommandHandler.include_in_llm_context is True
    assert FilesAllCommandHandler.include_in_llm_context is True
    assert AgentStatusCommandHandler.include_in_llm_context is True
    assert RunCommandHandler.include_in_llm_context is True

    # Opted-out (False)
    assert WebappCommandHandler.include_in_llm_context is False
    assert SessionRecoverCommandHandler.include_in_llm_context is False
    assert SessionResetCommandHandler.include_in_llm_context is False
    assert RebuildEnvCommandHandler.include_in_llm_context is False
