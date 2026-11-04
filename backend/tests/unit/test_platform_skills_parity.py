"""Parity: core's installed platform skills vs env-core's recognised ones.

Run:
    docker compose exec backend python -m pytest tests/unit/test_platform_skills_parity.py -v
"""
from __future__ import annotations

from app.services.environments.platform_protocol_files import (
    PLATFORM_SKILLS_KIT_DIR,
    PROTOCOL_FILES,
)
from core.server import prompt_generator
from core.server.skill_manifest import PLATFORM_SKILL_NAMES


def test_protocol_file_skills_match_env_core_platform_skill_names() -> None:
    prefix = f"{PLATFORM_SKILLS_KIT_DIR}/"
    installed = {p[len(prefix):] for p in PROTOCOL_FILES if p.startswith(prefix)}
    assert installed == set(PLATFORM_SKILL_NAMES)


def test_kit_dir_constants_match() -> None:
    assert PLATFORM_SKILLS_KIT_DIR == prompt_generator.PLATFORM_SKILL_KIT_DIR
