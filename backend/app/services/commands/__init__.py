"""
Agent session commands - quick, deterministic slash commands.

This module registers all available commands with the CommandService.
Import this module at startup to ensure commands are registered.
"""
from app.services.command_service import CommandService
from app.services.commands.files_command import FilesCommandHandler, FilesAllCommandHandler
from app.services.commands.session_recover_command import SessionRecoverCommandHandler
from app.services.commands.session_reset_command import SessionResetCommandHandler
from app.services.commands.webapp_command import WebappCommandHandler
from app.services.commands.rebuild_env_command import RebuildEnvCommandHandler

# Register all command handlers
CommandService.register(FilesCommandHandler())
CommandService.register(FilesAllCommandHandler())
CommandService.register(SessionRecoverCommandHandler())
CommandService.register(SessionResetCommandHandler())
CommandService.register(WebappCommandHandler())
CommandService.register(RebuildEnvCommandHandler())
