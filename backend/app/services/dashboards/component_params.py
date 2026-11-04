"""Tile params: validation, merging, resolution and hashing.

A component declares its params in ``component.json`` (``params``: name →
``{type, label?, values?, default?}``). A tile stores only the values the owner
(or the page, or the agent) chose; everything else falls back to the spec's
default. ``current`` (month) and ``today`` (date) are resolved to a value in
the owner's ``user.timezone`` (else UTC) before anything runs: the cloud has no
"host local time", and an endpoint never sees the words.

The value rules follow the kit's checker
(``skills/cinna-dashboards/scripts/check_component.py``, ``_default_ok``), and
the refusal sentences use its wording, so the owner, the page and the agent
read the same sentence for the same value.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
PARAM_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
TEXT_MAX = 1000
MAX_PARAMS = 32


@dataclass(frozen=True)
class ParamError:
    field: str
    message: str


def is_cleared(value: Any) -> bool:
    """A cleared value (``""`` or ``null``) removes the key: the default applies."""
    return value is None or value == ""


def owner_today(tz_name: str | None) -> date:
    """Today in the owner's timezone, else UTC."""
    tz: Any = UTC
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = UTC
    return datetime.now(tz).date()


def _specs(spec_params: Any) -> dict[str, dict]:
    if not isinstance(spec_params, dict):
        return {}
    return {name: spec for name, spec in spec_params.items() if isinstance(spec, dict)}


def _value_fits(kind: str, value: Any, values: Any) -> bool:
    if kind == "enum":
        return isinstance(values, list) and isinstance(value, str) and value in values
    if kind == "month":
        return isinstance(value, str) and (value == "current" or bool(MONTH_RE.fullmatch(value)))
    if kind == "date":
        return isinstance(value, str) and (value == "today" or bool(DATE_RE.fullmatch(value)))
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "text":
        return isinstance(value, str) and len(value) <= TEXT_MAX
    if kind == "date_range":
        # Host-defined presets: a short string.
        return isinstance(value, str) and len(value) <= 200
    return False


def validate(spec_params: Any, params: Any) -> list[ParamError]:
    """Refusals for a param set; ``[]`` when every value fits its spec.

    Cleared values are always accepted (they remove the key).
    """
    if not isinstance(params, dict):
        return [ParamError("params", "`params` must be an object of name → value.")]
    if len(params) > MAX_PARAMS:
        return [ParamError("params", f"`params` takes at most {MAX_PARAMS} values.")]
    specs = _specs(spec_params)
    errors: list[ParamError] = []
    for name, value in params.items():
        field = f"params.{name}"
        spec = specs.get(name)
        if spec is None:
            errors.append(ParamError(field, f"`{field}` is not a param of this component."))
            continue
        if is_cleared(value):
            continue
        kind = str(spec.get("type"))
        if not _value_fits(kind, value, spec.get("values")):
            errors.append(ParamError(field, f"`{field}` does not fit type {kind}."))
    return errors


def clean(params: dict[str, Any]) -> dict[str, Any]:
    """Drop cleared values: what is stored on the tile."""
    return {name: value for name, value in params.items() if not is_cleared(value)}


def merge(stored: dict[str, Any] | None, partial: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial change over the stored set; a cleared key goes back to its default."""
    merged = dict(stored or {})
    for name, value in partial.items():
        if is_cleared(value):
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


def _resolve_value(kind: str, value: Any, today: date) -> Any:
    if kind == "month" and value == "current":
        return today.strftime("%Y-%m")
    if kind == "date" and value == "today":
        return today.isoformat()
    return value


def resolve(spec_params: Any, stored: dict[str, Any] | None, tz_name: str | None) -> dict[str, Any]:
    """The values a run gets: each declared param's stored value, else its default.

    ``current``/``today`` are resolved in ``tz_name`` (else UTC). A param with
    neither a stored value nor a default is omitted. Stored keys the component
    no longer declares are dropped.
    """
    stored = stored or {}
    today = owner_today(tz_name)
    resolved: dict[str, Any] = {}
    for name, spec in _specs(spec_params).items():
        value = stored.get(name)
        if is_cleared(value):
            value = spec.get("default")
        if is_cleared(value):
            continue
        resolved[name] = _resolve_value(str(spec.get("type")), value, today)
    return resolved


def is_echo(
    spec_params: Any,
    stored: dict[str, Any] | None,
    key: str,
    value: Any,
    tz_name: str | None,
) -> bool:
    """Whether setting ``key`` to ``value`` changes nothing the tile runs with.

    True when the value resolves to what the tile already resolves to: the
    stored value itself, the default given back while nothing is stored, a
    ``null`` for a key at its default, or ``current``/``today`` for the
    value they stand for today. An echo neither runs nor counts.
    """
    before = resolve(spec_params, stored, tz_name)
    after = resolve(spec_params, merge(stored, {key: value}), tz_name)
    return before.get(key) == after.get(key)


def params_hash(resolved: dict[str, Any]) -> str:
    """sha256 of the resolved values (key order does not matter)."""
    canonical = json.dumps(resolved, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
