"""
Unit tests for the MCP prompts line parser.

Pure logic only — ``_parse_prompt_line`` parses agent ``example_prompts`` of the
form ``"slug: prompt text"``. No DB, no TestClient.

The connector-context-driven handler tests (list_prompts / get_prompt against a
real agent + connector) live in
``tests/api/mcp_integration/test_mcp_prompts.py``.
"""
from app.mcp.prompts import _parse_prompt_line


def test_parse_prompt_line_standard():
    """Standard 'slug: text' format is parsed correctly."""
    result = _parse_prompt_line("report_status: Send me status report")
    assert result == ("report_status", "Send me status report")


def test_parse_prompt_line_extra_colons():
    """Only the first colon is used as separator."""
    result = _parse_prompt_line("check_time: What time is it: now?")
    assert result == ("check_time", "What time is it: now?")


def test_parse_prompt_line_whitespace():
    """Whitespace around slug and text is trimmed."""
    result = _parse_prompt_line("  my_slug  :  Some prompt text  ")
    assert result == ("my_slug", "Some prompt text")


def test_parse_prompt_line_no_colon():
    """Lines without colon use the full line as both slug and text."""
    result = _parse_prompt_line("Just a prompt without colon")
    assert result == ("Just a prompt without colon", "Just a prompt without colon")


def test_parse_prompt_line_empty():
    """Empty lines return None."""
    assert _parse_prompt_line("") is None
    assert _parse_prompt_line("   ") is None


def test_parse_prompt_line_colon_only():
    """Line with only colon (empty slug and text) falls back to full line."""
    result = _parse_prompt_line(":")
    assert result == (":", ":")


def test_parse_prompt_line_empty_text_after_colon():
    """Colon with empty text after it uses full line as fallback."""
    result = _parse_prompt_line("slug:")
    assert result == ("slug:", "slug:")


def test_parse_prompt_line_empty_slug_before_colon():
    """Colon with empty slug before it uses full line as fallback."""
    result = _parse_prompt_line(": some text")
    assert result == (": some text", ": some text")
