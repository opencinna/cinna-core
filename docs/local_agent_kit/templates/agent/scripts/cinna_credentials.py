"""Portable credential access for an agent that runs locally and in the cloud.

Two runtimes, one call site:

* **Cloud** — the platform injects ``credentials/credentials.json``: a JSON array of
  ``{"id", "name", "type", "notes", "credential_data"}`` objects. This file is
  authoritative when it exists.
* **Local** — there is no injected file. Values live in ``credentials/.env`` as
  ``<ENV_PREFIX><FIELD IN UPPER CASE>`` variables, and ``cinna-agent.json``'s
  ``credentials[]`` block says which prefix and which fields belong to each slot.

Import this module from your scripts and never read either file directly::

    from cinna_credentials import get_credential

    imap = get_credential("email_imap")          # by type
    imap = get_credential("billing-inbox")       # or by slot name
    host, port = imap["host"], imap["port"]

Security: this module returns secret values to *your script*. Never print, log or
write them anywhere. There is deliberately no ``__main__`` block and no dump helper.

Standard library only. Python 3.11+.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "CredentialError",
    "MissingCredentialError",
    "agent_root",
    "get_credential",
    "require_credential",
    "list_credential_slots",
]

_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}


class CredentialError(RuntimeError):
    """The credential store could not be read or understood."""


class MissingCredentialError(CredentialError):
    """No credential matched the requested name or type."""


def agent_root() -> Path:
    """Return the agent folder root.

    Resolved from this file's location (``<root>/scripts/cinna_credentials.py``), so
    it is correct no matter which directory the script was launched from.
    """
    return Path(__file__).resolve().parent.parent


def _read_manifest() -> dict[str, Any]:
    path = agent_root() / "cinna-agent.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialError(f"cinna-agent.json is not readable JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def list_credential_slots() -> list[dict[str, Any]]:
    """Return the ``credentials[]`` specs declared in ``cinna-agent.json``.

    Names and types only — never any value.
    """
    slots = _read_manifest().get("credentials")
    return [s for s in slots if isinstance(s, dict)] if isinstance(slots, list) else []


def _coerce(field: str, raw: str) -> Any:
    """Turn a .env string into the type the platform would have supplied."""
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    if field == "port" or field.endswith("_port"):
        try:
            return int(raw.strip())
        except ValueError:
            return raw
    return raw


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, ``#`` comments, optional quotes."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(f"cannot read {path.name}: {exc}") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _from_cloud(name_or_type: str) -> dict[str, Any] | None:
    path = agent_root() / "credentials" / "credentials.json"
    if not path.is_file():
        return None
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialError(f"credentials.json is not readable JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise CredentialError("credentials.json must contain a JSON array")

    by_name = [e for e in entries if isinstance(e, dict) and e.get("name") == name_or_type]
    by_type = [e for e in entries if isinstance(e, dict) and e.get("type") == name_or_type]
    match = (by_name or by_type)
    if not match:
        return None
    data = match[0].get("credential_data")
    return dict(data) if isinstance(data, dict) else {}


def _from_local(name_or_type: str) -> dict[str, Any] | None:
    slots = list_credential_slots()
    candidates = [s for s in slots if s.get("name") == name_or_type]
    if not candidates:
        candidates = [s for s in slots if s.get("type") == name_or_type]
    if not candidates:
        return None
    slot = candidates[0]

    prefix = str(slot.get("env_prefix") or "")
    fields = [str(f) for f in slot.get("fields") or [] if f]
    if not prefix or not fields:
        raise CredentialError(
            f"credential slot '{slot.get('name')}' needs env_prefix and fields "
            "in cinna-agent.json to be readable from credentials/.env"
        )

    env_path = agent_root() / "credentials" / ".env"
    file_values = _parse_env_file(env_path) if env_path.is_file() else {}

    resolved: dict[str, Any] = {}
    for field in fields:
        var = f"{prefix}{field.upper()}"
        # A real environment variable wins over the file, so CI and one-off runs
        # can override without editing .env.
        raw = os.environ.get(var, file_values.get(var))
        if raw is None or raw == "":
            continue
        resolved[field] = _coerce(field, raw)
    return resolved or None


def get_credential(name_or_type: str) -> dict[str, Any] | None:
    """Return one credential's data, or ``None`` when it is not configured.

    Matching is by slot ``name`` first, then by platform ``type``. In the cloud the
    injected ``credentials.json`` is used; locally the values are assembled from
    ``credentials/.env`` using the slot's ``env_prefix`` and ``fields``.
    """
    if not name_or_type:
        raise ValueError("name_or_type must be a non-empty string")
    cloud = _from_cloud(name_or_type)
    if cloud is not None:
        return cloud
    return _from_local(name_or_type)


def require_credential(name_or_type: str) -> dict[str, Any]:
    """Like :func:`get_credential`, but raise when the credential is missing.

    The message names the slot and where to fix it — never a value.
    """
    found = get_credential(name_or_type)
    if not found:
        raise MissingCredentialError(
            f"credential '{name_or_type}' is not configured. "
            "Locally: add its variables to credentials/.env (see credentials/README.md). "
            "In the cloud: share a credential of that name or type with this agent."
        )
    return found
