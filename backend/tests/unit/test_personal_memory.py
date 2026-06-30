"""Unit tests for PromptGenerator personal-memory reader and prompt injection.

Covers ``_load_personal_memory`` (file I/O on a tmp_path tree) and its
integration into ``generate_conversation_mode_prompt`` and
``generate_building_mode_prompt``.

Pure filesystem tests — no database, no HTTP client, no live agent environment.

Notes
-----
End-to-end prompt injection (mode prompt surfaced to a live agent environment
during a streaming session) is exercised through integration tests in
``tests/api/agents/``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.server.prompt_generator import (
    PERSONAL_MEMORY_MAX_CHARS,
    PromptGenerator,
    _PERSONAL_MEMORY_GUIDANCE,
    _PERSONAL_MEMORY_TRUNCATION_NOTE,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_pg(tmp_path: Path) -> PromptGenerator:
    """Return a PromptGenerator whose workspace_dir is *tmp_path*.

    ``/app/core/prompts/BUILDING_AGENT.md`` is absent in the unit test
    environment, so ``pg.building_agent_prompt`` will be ``None`` after
    construction.  Set it explicitly on the returned instance to exercise the
    real building-mode assembly path that includes memory injection.
    """
    return PromptGenerator(workspace_dir=str(tmp_path))


def _make_memory_dir(tmp_path: Path) -> Path:
    """Create and return ``tmp_path/app-data/memory/``."""
    d = tmp_path / "app-data" / "memory"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# 1. Missing memory directory — true no-op
# ---------------------------------------------------------------------------


class TestMissingMemoryDir:

    def test_no_dir_load_returns_none(self, tmp_path: Path) -> None:
        """``_load_personal_memory`` returns None when ``app-data/memory/`` does not exist."""
        pg = _make_pg(tmp_path)
        assert pg._load_personal_memory() is None

    def test_no_dir_conversation_prompt_has_no_memory_header(self, tmp_path: Path) -> None:
        """Missing memory directory is a true no-op — the memory header never appears."""
        pg = _make_pg(tmp_path)
        prompt = pg.generate_conversation_mode_prompt()
        assert "## Personalization / User Memory" not in prompt


# ---------------------------------------------------------------------------
# 2. Directory exists but holds no usable content
# ---------------------------------------------------------------------------


class TestEmptyOrWhitespaceMemory:

    def test_empty_dir_returns_none(self, tmp_path: Path) -> None:
        """An empty ``app-data/memory/`` directory yields None (no .md files)."""
        _make_memory_dir(tmp_path)
        pg = _make_pg(tmp_path)
        assert pg._load_personal_memory() is None

    def test_whitespace_only_file_returns_none(self, tmp_path: Path) -> None:
        """A sole file whose stripped content is empty is skipped → returns None."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("   \n\t  ")
        pg = _make_pg(tmp_path)
        assert pg._load_personal_memory() is None

    def test_multiple_whitespace_only_files_returns_none(self, tmp_path: Path) -> None:
        """Several whitespace-only files together still yield None."""
        d = _make_memory_dir(tmp_path)
        (d / "a.md").write_text("")
        (d / "b.md").write_text("   ")
        (d / "c.md").write_text("\n\n")
        pg = _make_pg(tmp_path)
        assert pg._load_personal_memory() is None


# ---------------------------------------------------------------------------
# 3. Single small file — body structure
# ---------------------------------------------------------------------------


class TestSingleSmallFile:

    def test_body_contains_guidance_paragraph(self, tmp_path: Path) -> None:
        """Returned body opens with the fixed ``_PERSONAL_MEMORY_GUIDANCE`` paragraph."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("I prefer concise answers.")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert _PERSONAL_MEMORY_GUIDANCE in body

    def test_body_contains_filename_label(self, tmp_path: Path) -> None:
        """Each file appears under a ``### <filename>`` sub-section label."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("I prefer concise answers.")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert "### MEMORY.md" in body

    def test_body_contains_file_content(self, tmp_path: Path) -> None:
        """The file's content appears verbatim in the returned body."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("I prefer concise answers.")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert "I prefer concise answers." in body

    def test_conversation_prompt_has_memory_section_header_and_content(
        self, tmp_path: Path
    ) -> None:
        """With memory present, the conversation prompt includes the section header
        followed by the file content."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("User prefers dark mode.")
        pg = _make_pg(tmp_path)
        prompt = pg.generate_conversation_mode_prompt()
        assert "## Personalization / User Memory" in prompt
        assert "User prefers dark mode." in prompt

    def test_no_truncation_note_for_small_file(self, tmp_path: Path) -> None:
        """A file well within the character cap produces no truncation note."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("Small memo.")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert _PERSONAL_MEMORY_TRUNCATION_NOTE not in body


# ---------------------------------------------------------------------------
# 4. Multiple .md files — ordering and labelling
# ---------------------------------------------------------------------------


class TestMultipleFiles:

    def test_all_files_appear_with_their_labels(self, tmp_path: Path) -> None:
        """Every non-empty .md file gets its own ``### <name>`` label in the body."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("Global memory.")
        (d / "b.md").write_text("Section B notes.")
        (d / "A.md").write_text("Section A notes.")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert "### MEMORY.md" in body
        assert "### b.md" in body
        assert "### A.md" in body

    def test_files_sorted_case_insensitively(self, tmp_path: Path) -> None:
        """Files are ordered by (name.lower(), name) — A.md < b.md < MEMORY.md."""
        d = _make_memory_dir(tmp_path)
        # Sort keys:
        #   A.md      → ("a.md",      "A.md")
        #   b.md      → ("b.md",      "b.md")
        #   MEMORY.md → ("memory.md", "MEMORY.md")
        (d / "MEMORY.md").write_text("memory entry")
        (d / "b.md").write_text("b entry")
        (d / "A.md").write_text("A entry")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        idx_a = body.index("### A.md")
        idx_b = body.index("### b.md")
        idx_m = body.index("### MEMORY.md")
        assert idx_a < idx_b < idx_m

    def test_whitespace_only_file_skipped_among_others(self, tmp_path: Path) -> None:
        """An empty/whitespace file is skipped; the rest are still included."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("Real memory.")
        (d / "empty.md").write_text("   ")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert "### MEMORY.md" in body
        assert "### empty.md" not in body


# ---------------------------------------------------------------------------
# 5. Non-.md files are ignored
# ---------------------------------------------------------------------------


class TestNonMdFilesIgnored:

    def test_txt_and_json_files_not_injected(self, tmp_path: Path) -> None:
        """Only ``.md`` files are picked up; other extensions are silently ignored."""
        d = _make_memory_dir(tmp_path)
        (d / "notes.txt").write_text("This is a text note.")
        (d / "data.json").write_text('{"key": "value"}')
        (d / "MEMORY.md").write_text("Actual memory content.")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert "notes.txt" not in body
        assert "data.json" not in body
        assert "### MEMORY.md" in body
        assert "Actual memory content." in body

    def test_directory_entries_not_injected(self, tmp_path: Path) -> None:
        """A subdirectory inside the memory dir is silently ignored."""
        d = _make_memory_dir(tmp_path)
        (d / "subdir").mkdir()
        (d / "MEMORY.md").write_text("Main note.")
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert "### MEMORY.md" in body
        # No label for the subdirectory
        assert "### subdir" not in body


# ---------------------------------------------------------------------------
# 6. Oversize single file — truncated slice (regression guard for the fixed bug)
# ---------------------------------------------------------------------------


class TestTruncationSingleFile:

    def test_oversize_single_file_not_dropped_to_none(self, tmp_path: Path) -> None:
        """When the only memory file exceeds PERSONAL_MEMORY_MAX_CHARS, it is sliced
        and returned — NOT silently dropped to None.

        This is the primary regression guard for the fixed bug: a very large
        single file previously caused ``_load_personal_memory`` to return None,
        silently discarding all stored memory.
        """
        d = _make_memory_dir(tmp_path)
        content = "x" * (PERSONAL_MEMORY_MAX_CHARS + 500)
        (d / "MEMORY.md").write_text(content)
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None  # must NOT be None — the fixed-bug guard

    def test_oversize_single_file_truncation_note_present(self, tmp_path: Path) -> None:
        """Truncated body always includes the truncation note."""
        d = _make_memory_dir(tmp_path)
        content = "y" * (PERSONAL_MEMORY_MAX_CHARS + 500)
        (d / "MEMORY.md").write_text(content)
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert _PERSONAL_MEMORY_TRUNCATION_NOTE in body

    def test_oversize_single_file_label_present(self, tmp_path: Path) -> None:
        """The ``### <filename>`` label is present even when content is truncated."""
        d = _make_memory_dir(tmp_path)
        content = "z" * (PERSONAL_MEMORY_MAX_CHARS + 500)
        (d / "MEMORY.md").write_text(content)
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        assert "### MEMORY.md" in body

    def test_oversize_single_file_slice_is_exactly_cap_chars(self, tmp_path: Path) -> None:
        """The included block slice is ``block[:PERSONAL_MEMORY_MAX_CHARS]`` verbatim."""
        d = _make_memory_dir(tmp_path)
        content = "q" * (PERSONAL_MEMORY_MAX_CHARS + 500)
        (d / "MEMORY.md").write_text(content)
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        # Build the raw block the same way the implementation does
        raw_block = f"### MEMORY.md\n\n{content}"
        expected_slice = raw_block[:PERSONAL_MEMORY_MAX_CHARS]
        assert expected_slice in body


# ---------------------------------------------------------------------------
# 7. Multiple files — first fits, later file overflows (break-on-overflow)
# ---------------------------------------------------------------------------


class TestTruncationMultipleFiles:

    def test_first_file_present_second_dropped_on_overflow(self, tmp_path: Path) -> None:
        """When the second file's block would push the total past the cap it is
        dropped in its entirety (not sliced); the first file remains complete
        and the truncation note is appended.
        """
        d = _make_memory_dir(tmp_path)
        # a_first.md: small, well within budget
        small_content = "Short note from file a."
        (d / "a_first.md").write_text(small_content)
        # b_second.md: large enough to trigger overflow after a_first.md is added
        (d / "b_second.md").write_text("L" * PERSONAL_MEMORY_MAX_CHARS)
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        # First file fully present
        assert "### a_first.md" in body
        assert small_content in body
        # Second file entirely absent (dropped whole, not sliced)
        assert "### b_second.md" not in body
        # Truncation note signals the omission
        assert _PERSONAL_MEMORY_TRUNCATION_NOTE in body

    def test_first_file_included_in_full_when_second_overflows(self, tmp_path: Path) -> None:
        """The first file's content is NOT trimmed — only subsequent files are
        dropped whole when the budget is exhausted."""
        d = _make_memory_dir(tmp_path)
        first_content = "Full note: remember the user's name is Alex."
        (d / "a_note.md").write_text(first_content)
        (d / "b_huge.md").write_text("H" * (PERSONAL_MEMORY_MAX_CHARS + 100))
        pg = _make_pg(tmp_path)
        body = pg._load_personal_memory()
        assert body is not None
        # The first file's complete content is in the body
        assert first_content in body


# ---------------------------------------------------------------------------
# 8. Building mode injection
# ---------------------------------------------------------------------------


class TestBuildingModeInjection:

    def test_with_memory_and_building_prompt_set_append_has_header(
        self, tmp_path: Path
    ) -> None:
        """When ``building_agent_prompt`` is set and memory is present, the
        ``append`` string in the returned dict contains the memory section."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("User prefers Python over JavaScript.")
        pg = _make_pg(tmp_path)
        # Force the real assembly path by setting building_agent_prompt directly
        pg.building_agent_prompt = "You are a building agent."
        result = pg.generate_building_mode_prompt()
        assert isinstance(result, dict)
        assert result.get("type") == "preset"
        assert result.get("preset") == "claude_code"
        assert "append" in result
        assert "## Personalization / User Memory" in result["append"]
        assert "User prefers Python over JavaScript." in result["append"]

    def test_with_no_memory_append_has_no_memory_header(self, tmp_path: Path) -> None:
        """When there is no memory content the building-mode ``append`` does NOT
        contain the memory section header (true no-op)."""
        pg = _make_pg(tmp_path)
        pg.building_agent_prompt = "You are a building agent."
        result = pg.generate_building_mode_prompt()
        assert "append" in result
        assert "## Personalization / User Memory" not in result["append"]

    def test_none_building_agent_prompt_returns_minimal_preset_no_append(
        self, tmp_path: Path
    ) -> None:
        """When ``building_agent_prompt`` is None (BUILDING_AGENT.md absent from
        the unit test environment), ``generate_building_mode_prompt`` returns the
        minimal preset dict — no ``append`` key, even when memory is present."""
        d = _make_memory_dir(tmp_path)
        (d / "MEMORY.md").write_text("Some personal memory.")
        pg = _make_pg(tmp_path)
        assert pg.building_agent_prompt is None  # confirm the unit-env default
        result = pg.generate_building_mode_prompt()
        assert result == {"type": "preset", "preset": "claude_code"}
        assert "append" not in result


# ---------------------------------------------------------------------------
# 9. Defensive — _load_personal_memory never raises
# ---------------------------------------------------------------------------


class TestLoadPersonalMemoryDefensive:

    def test_memory_path_is_file_not_dir_returns_none(self, tmp_path: Path) -> None:
        """When ``app-data/memory`` exists as a regular file rather than a directory,
        ``_load_personal_memory`` returns None without raising.

        The implementation's ``memory_dir.is_dir()`` guard handles this path cleanly.
        """
        app_data = tmp_path / "app-data"
        app_data.mkdir(parents=True)
        # Deliberately create "memory" as a plain file
        (app_data / "memory").write_text("I am a file, not a directory.")
        pg = _make_pg(tmp_path)
        result = pg._load_personal_memory()
        assert result is None
