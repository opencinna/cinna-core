"""``/skills`` — list the agent skills the model can invoke.

Reads the cached index (``AgentSkillsService``) rather than the container, so
the command answers instantly and works on a sleeping agent. The rows are the
same ones the agent page shows: local ``skills/`` folders plus the skills that
arrived with installed plugins.

Output is a ``document`` and goes into the LLM context: a user asking "what can
you do" gets the index, and so does the model reading the transcript afterwards.
"""
import logging

from app.services.agents.command_service import (
    CommandContext,
    CommandHandler,
    CommandResult,
)

logger = logging.getLogger(__name__)

_NO_SKILLS_COPY = (
    "This agent has no skills yet. Skills are folders under `skills/` — "
    "`skills/<name>/SKILL.md` with a `name` and a `description` — that the "
    "agent can invoke by name."
)

_REBUILD_COPY = (
    "Rebuild the environment to enable skills — this container was built "
    "before agent skills existed."
)


def _cell(text: str) -> str:
    """Make one value safe to drop into a markdown table cell.

    A skill description is publisher-authored free text; an unescaped ``|`` in
    it would split the row and shift every later column, so the table would lie
    about which skill does what. The NAME goes through here too: a valid one
    cannot hold a pipe, but a failed entry carries the raw directory name, and
    a failed entry is precisely the row this is protecting.
    """
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _invoke_cell(entry) -> str:
    """How a person invokes this skill, or why they can't."""
    if entry.error is not None:
        return "unavailable"
    if not entry.user_invocable:
        return "model only"
    return f"`/{entry.name}`"



def _source_cell(entry) -> str:
    if entry.source == "plugin" and entry.plugin_ref:
        return f"plugin: {entry.plugin_ref}"
    return entry.source


def _describe(entry) -> str:
    """Description cell, with the problem in front of it when there is one."""
    description = entry.description or ""
    if entry.error is not None:
        return f"⚠ {entry.error.message} {description}".strip()
    if entry.warning is not None:
        return f"⚠ {entry.warning.message} {description}".strip()
    return description


class SkillsCommandHandler(CommandHandler):
    """Handler for ``/skills`` — the agent's skill index as a markdown table."""

    streams: bool = False
    include_in_llm_context: bool = True
    requires_running_environment: bool = False

    @property
    def name(self) -> str:
        return "/skills"

    @property
    def description(self) -> str:
        return "List the skills this agent can use"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        if args.strip():
            return CommandResult(
                content="`/skills` takes no arguments.",
                is_error=True,
            )

        from app.core.db import create_session
        from app.models import AgentEnvironment
        from app.services.agents.agent_skills_service import (
            ERROR_ADAPTER_ERROR,
            AgentSkillsService,
        )

        with create_session() as db:
            environment = db.get(AgentEnvironment, context.environment_id)
            if not environment:
                return CommandResult(
                    content="Environment not found.",
                    is_error=True,
                )
            entries = AgentSkillsService.get_cached_entries(environment)
            skills_error = environment.skills_error

        if not entries:
            # A pre-feature container reports nothing AND cannot: say which it
            # is, because the two need different actions from the user.
            if skills_error == ERROR_ADAPTER_ERROR:
                return CommandResult(content=_REBUILD_COPY, is_error=True)
            # Not an error: an agent that has no skills is an empty state, and
            # colouring it red would tell the user something is broken.
            return CommandResult(content=_NO_SKILLS_COPY)

        lines = [
            "The model sees each skill's name and description, and reads the "
            "full instructions only when it uses one.",
            "",
            "| Skill | Source | What it does | Invoke |",
            "|-------|--------|--------------|--------|",
        ]
        # Flagged rows last: the table is read top-down for "what can this
        # agent do", and a broken skill is not an answer to that question.
        ordered = sorted(entries, key=lambda e: (e.error is not None, e.name))
        for entry in ordered:
            lines.append(
                f"| `{_cell(entry.name)}` | {_cell(_source_cell(entry))} | "
                f"{_cell(_describe(entry))} | {_invoke_cell(entry)} |"
            )

        return CommandResult(content="\n".join(lines), display="document")
