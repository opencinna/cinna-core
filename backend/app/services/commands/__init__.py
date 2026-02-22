"""
Agent session commands - quick, deterministic slash commands.

This module registers all available commands with the CommandService.
Import this module at startup to ensure commands are registered.
"""
from app.services.command_service import CommandService
from app.services.commands.files_command import FilesCommandHandler, FilesAllCommandHandler

# Register all command handlers
CommandService.register(FilesCommandHandler())
CommandService.register(FilesAllCommandHandler())
