"""Where a marketplace plugin entry's files come from, for its snapshot.

Pure: no network, no database. :func:`resolve_source` turns one entry (a parsed
entry dict during a sync, or a stored row for the worker) into a
:class:`SnapshotSource` or an :class:`Unsupported` verdict.

Claude rows are read from ``config["source"]``, never from ``source_type``: the
Claude parser stores ``git-subdir`` entries as ``local`` with the external path
(the cloud install path relies on that and is left alone), so the stored
columns cannot tell a local entry from an external one. Codex rows use the
stored columns, which that parser fills correctly.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.services.knowledge.git_operations import _validate_fetch_target
from app.services.plugins.llm_plugin_service import LLMPluginService

#: Parser verdicts that also mean "nothing to fetch".
PARSER_UNFETCHABLE = frozenset({"unsafe_path", "unknown_source", "npm_source"})

_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


@dataclass(frozen=True)
class SnapshotSource:
    """``kind='local'``: ``subdir`` of the marketplace repository itself.
    ``kind='git'``: ``subdir`` of the external public repository at ``url``,
    pinned to ``sha`` or following ``ref`` (None: the remote's HEAD)."""

    kind: str
    subdir: str
    key: str
    url: str | None = None
    sha: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class Unsupported:
    reason: str


def row_fields(row: Any) -> dict[str, Any]:
    """The fields :func:`resolve_source` reads, off a stored plugin row."""
    return {
        "name": row.name,
        "config": row.config,
        "source_type": row.source_type,
        "source_url": row.source_url,
        "source_path": row.source_path,
        "source_commit_hash": row.source_commit_hash,
        "source_branch": row.source_branch,
        "supported": row.supported,
        "unsupported_reason": row.unsupported_reason,
    }


def normalise_subdir(raw: str | None, *, entry_name: str) -> str | None:
    """A repo-relative directory without ``./`` or slashes at either end.

    ``""`` is the repository root; None means the path escapes the repository.
    """
    safe = LLMPluginService._safe_entry_path(raw or "", entry_name=entry_name)
    if safe is None:
        return None
    safe = safe.strip("/")
    return "" if safe in ("", ".") else safe


def https_url(raw: str | None) -> str | None:
    """``raw`` as a fetchable public HTTPS URL, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    url = (LLMPluginService._normalize_public_git_url(raw.strip()) or "").strip()
    return url if url.startswith("https://") else None


def git_source_key(url: str, subdir: str) -> str:
    """``<canonical url>#<subdir>``: SSH and HTTPS spellings share one key."""
    return f"{LLMPluginService._canonical_repository_url(url)}#{subdir}"


def local_source_key(subdir: str) -> str:
    return f"local:{subdir}"


def _str_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _git(url_raw: Any, subdir_raw: Any, sha: Any, ref: Any, name: str):
    url = https_url(url_raw)
    if url is None:
        return Unsupported("unknown_source")
    subdir = normalise_subdir(
        subdir_raw if isinstance(subdir_raw, str) else "", entry_name=name
    )
    if subdir is None:
        return Unsupported("unsafe_path")
    sha, ref = _str_or_none(sha), _str_or_none(ref)
    try:
        # Same rule the fetch enforces: never hand git an option or refspec.
        _validate_fetch_target(sha, ref)
    except ValueError:
        return Unsupported("unknown_source")
    return SnapshotSource(
        kind="git",
        url=url,
        subdir=subdir,
        sha=sha,
        ref=ref,
        key=git_source_key(url, subdir),
    )


def _local(path_raw: Any, name: str):
    if not isinstance(path_raw, str) or not path_raw.strip():
        return Unsupported("unknown_source")
    subdir = normalise_subdir(path_raw, entry_name=name)
    if subdir is None:
        return Unsupported("unsafe_path")
    return SnapshotSource(kind="local", subdir=subdir, key=local_source_key(subdir))


def _resolve_claude(entry: Mapping[str, Any], name: str):
    config = entry.get("config")
    source = config.get("source") if isinstance(config, dict) else None
    if isinstance(source, str):
        return _local(source, name)
    if not isinstance(source, dict):
        return Unsupported("unknown_source")
    kind = source.get("source")
    ref = source.get("ref") or source.get("branch")
    if kind in ("url", "git-subdir"):
        # Some ``url`` entries carry a ``path`` too (the plugin lives in a
        # subfolder); honoured like ``git-subdir``'s.
        return _git(
            source.get("url"), source.get("path") or "", source.get("sha"), ref, name
        )
    if kind == "github":
        repo = source.get("repo")
        if not isinstance(repo, str) or not _GITHUB_REPO_RE.match(repo.strip()):
            return Unsupported("unknown_source")
        return _git(
            f"https://github.com/{repo.strip()}.git",
            source.get("path") or "",
            source.get("sha"),
            ref,
            name,
        )
    if kind is None and "path" in source:
        # ``{"path": "./plugins/x"}``: the parser's local-object form.
        return _local(source.get("path"), name)
    return Unsupported("unknown_source")


def _resolve_codex(entry: Mapping[str, Any], name: str):
    if not entry.get("supported", True) and (
        entry.get("unsupported_reason") in PARSER_UNFETCHABLE
    ):
        return Unsupported(entry["unsupported_reason"])
    source_type = entry.get("source_type")
    source_type = getattr(source_type, "value", source_type)
    if source_type == "url":
        return _git(
            entry.get("source_url"),
            entry.get("source_path") or "",
            entry.get("source_commit_hash"),
            entry.get("source_branch"),
            name,
        )
    return _local(entry.get("source_path"), name)


def resolve_source(
    entry: Mapping[str, Any], fmt: str
) -> SnapshotSource | Unsupported:
    """Where ``entry`` (a parsed entry dict or :func:`row_fields`) lives."""
    name = str(entry.get("name") or "")
    if fmt == "claude":
        return _resolve_claude(entry, name)
    if fmt == "codex":
        return _resolve_codex(entry, name)
    return Unsupported("unknown_source")
