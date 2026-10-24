"""Text helpers shared by the addons directory adapters."""


def summary_line(text: str | None, limit: int = 280) -> str | None:
    """A one-line summary off a (possibly markdown) description."""
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return None
