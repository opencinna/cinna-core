#!/usr/bin/env python3
"""Credential access for this agent — the same call locally and in the cloud.

Precedence: the injected CINNA_CREDENTIALS_PATH array, credentials/credentials.json
(array or envelope), the legacy root object, environment, then credentials/.env.
Attached entries replace a whole slot; placeholders never merge environment fields.
An attached entry the host could not deliver carries ``unavailable_reason`` (the
Core refusal reason, e.g. ``local_use_not_allowed``); it blocks the env fallback
like a placeholder, and the error names the reason.

Use one call either way::

    from cinna_credentials import get_credential

    password = get_credential("Odoo", "password")
    odoo = get_credential("Odoo")            # every field of the slot, as a dict

Call another agent's REST API through an issued ``agent_api`` key::

    from cinna_credentials import agent_api_session

    api = agent_api_session("crm-agent-api")
    deals = api.get_json("/deals", params={"stage": "open"})

Never print, log or pass a returned value anywhere but the client that needs it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parents[1]
HELPER_VERSION = "1.14.0"
CLOUD_CREDENTIALS = AGENT_ROOT / "credentials" / "credentials.json"
LOCAL_ENV = AGENT_ROOT / "credentials" / ".env"
MANIFEST = AGENT_ROOT / "cinna-agent.json"


class CredentialError(RuntimeError):
    """A declared credential, or one of its fields, is not configured."""


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _slots() -> list[dict]:
    manifest = _read_json(MANIFEST)
    if not isinstance(manifest, dict):
        return []
    slots = manifest.get("credentials")
    return [s for s in slots if isinstance(s, dict)] if isinstance(slots, list) else []


def _slot(name: str) -> dict:
    slots = _slots()
    for slot in slots:
        if slot.get("name") == name:
            return slot
    # A type or service_uri names a slot only when it is unambiguous; with two
    # slots of one type the first would silently resolve the wrong credential.
    matches = [s for s in slots if s.get("type") == name or s.get("service_uri") == name]
    if len(matches) > 1:
        names = ", ".join(repr(s.get("name")) for s in matches)
        raise CredentialError(
            f"credential {name!r} matches several declared slots ({names}). "
            f"Ask for it by slot name."
        )
    return matches[0] if matches else {}


# Why the host could not deliver an attached credential (Core refusal reasons).
_UNAVAILABLE_HINTS = {
    "local_use_not_allowed": "its owner has not allowed use on this computer",
    "no_access": "it is no longer shared with you",
    "not_found": "it no longer exists",
    "unsupported_type": "this credential type cannot be used on this computer",
    # Issued agent_api keys (cloud and Desktop alike); ``key_status`` is the detail.
    "key_revoked": "the key was revoked by its issuer or its producer is gone",
    "key_expired": "the key has expired",
    "key_paused": "the producer has paused API access",
    # Google OAuth types (contract 1.14.0): core holds the refresh token.
    "pending_authorization": "the Google account has not been connected yet",
    "reauthorization_required": "Google needs the account reconnected",
    "token_unavailable": "Google did not issue a token just now; try again",
}


def _unavailable_hint(entries: list[dict]) -> str | None:
    for entry in entries:
        reason = entry.get("unavailable_reason")
        if isinstance(reason, str) and reason:
            hint = _UNAVAILABLE_HINTS.get(reason, "it could not be delivered")
            status = entry.get("key_status")
            if isinstance(status, str) and status:
                return f"{hint} ({reason}, key_status {status})"
            return f"{hint} ({reason})"
    return None


def derive_env_prefix(name: str) -> str:
    """``Vendor Portal`` -> ``VENDOR_PORTAL_``. Used when a slot declares none."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    if not cleaned:
        raise CredentialError("credential name has no usable characters")
    if not cleaned[0].isalpha():
        cleaned = "C_" + cleaned
    return cleaned + "_"


def _env_prefix(name: str) -> str:
    declared = _slot(name).get("env_prefix")
    return declared if isinstance(declared, str) and declared else derive_env_prefix(name)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader: ``KEY=value``, optional ``export``, # comments,
    optional single or double quotes. No interpolation, by design."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _entries() -> list[dict]:
    paths = []
    if os.environ.get("CINNA_CREDENTIALS_PATH"):
        paths.append(Path(os.environ["CINNA_CREDENTIALS_PATH"]))
    paths.append(CLOUD_CREDENTIALS)
    for path in paths:
        data = _read_json(path)
        if isinstance(data, dict):
            data = data.get("credentials")
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    return []


def _match(entries: list[dict]) -> dict | None:
    filled = [e for e in entries if not e.get("is_placeholder") and not e.get("unavailable_reason")]
    if not filled:
        return {} if entries else None
    data = filled[0].get("credential_data")
    return dict(data) if isinstance(data, dict) else None


def by_slot(slot: str, credential_type: str | None = None) -> dict | None:
    """Strict service_uri lookup; never falls back to names or types."""
    return _match([e for e in _entries() if e.get("service_uri") == slot
                   and (credential_type is None or e.get("type") == credential_type)])


def require_slot(slot: str, credential_type: str | None = None) -> dict:
    result = by_slot(slot, credential_type)
    if not result:
        hint = _unavailable_hint([e for e in _entries() if e.get("service_uri") == slot
                                  and (credential_type is None or e.get("type") == credential_type)])
        if hint:
            raise CredentialError(f"credential slot {slot!r} is unavailable: {hint}.")
        raise CredentialError(f"credential slot {slot!r} is not configured. Attach a filled credential to this agent.")
    return result


def _candidate_groups(name: str) -> tuple[list[dict], ...]:
    entries = _entries()
    slot = _slot(name)
    if slot:
        # A declared name identifies a service, not every token of its type.
        candidates = [e for e in entries if e.get("type") == slot.get("type")]
        explicit = slot.get("service_uri")
        return ([e for e in candidates if e.get("service_uri") == explicit],) if explicit else (
            [e for e in candidates if e.get("name") == slot.get("name")],
            [e for e in candidates if e.get("service_uri") == slot.get("name")],
        )
    # Keep explicit type lookups compatible for callers without manifest slots.
    return ([e for e in entries if e.get("name") == name],
            [e for e in entries if e.get("service_uri") == name],
            [e for e in entries if e.get("type") == name])


def unavailable_reason(name: str) -> str | None:
    """Why an attached credential for ``name`` was not delivered, or None."""
    for candidates in _candidate_groups(name):
        if candidates:
            return _unavailable_hint(candidates)
    return None


def _cloud_payload(name: str) -> dict[str, Any] | None:
    for candidates in _candidate_groups(name):
        result = _match(candidates)
        if result is not None:
            return result
    data = _read_json(AGENT_ROOT / "credentials.json")
    if isinstance(data, dict) and isinstance(data.get(name), dict):
        return data[name]
    return None


def agent_root() -> Path:
    return AGENT_ROOT


def _coerce(field: str, raw: str) -> Any:
    if field == "port" or field.endswith("_port"):
        try:
            return int(raw.strip())
        except ValueError:
            return raw
    if field.startswith(("is_", "use_")):
        if raw.strip().lower() in ("true", "yes", "1", "on"):
            return True
        if raw.strip().lower() in ("false", "no", "0", "off"):
            return False
    return raw


def _local_payload(name: str) -> dict[str, Any] | None:
    prefix = _env_prefix(name)
    merged: dict[str, str] = {**_parse_env_file(LOCAL_ENV), **os.environ}
    payload = {
        key[len(prefix):].lower(): _coerce(key[len(prefix):].lower(), value)
        for key, value in merged.items()
        if key.startswith(prefix) and value != ""
    }
    return payload or None


def get_credential(
    name: str,
    field: str | None = None,
    *,
    default: Any = None,
    required: bool = False,
) -> Any:
    """Return one field of a credential slot, or the whole slot as a dict.

    Raises :class:`CredentialError` when the slot (or the field) is missing and
    ``required`` is true; otherwise returns ``default``.
    """
    payload = _cloud_payload(name)
    if payload is None:
        payload = _local_payload(name)
    if not payload:
        if required:
            hint = unavailable_reason(name)
            if hint:
                raise CredentialError(f"credential {name!r} is unavailable: {hint}.")
            raise CredentialError(
                f"credential {name!r} is not configured. "
                f"Set {_env_prefix(name)}<FIELD> in credentials/.env "
                f"(see credentials/README.md)."
            )
        return default
    if field is None:
        return payload
    value = payload.get(field)
    if value in (None, ""):
        if required:
            raise CredentialError(
                f"credential {name!r} has no {field!r}. "
                f"Set {_env_prefix(name)}{field.upper()} in credentials/.env."
            )
        return default
    return value


def require_credential(name: str, field: str | None = None) -> Any:
    return get_credential(name, field, required=True)


def list_credential_slots() -> list[dict]:
    return _slots()


MissingCredentialError = CredentialError


def has_credential(name: str, field: str | None = None) -> bool:
    """True when the slot (or that field of it) has a value. Reads no value out."""
    return get_credential(name, field, required=False) not in (None, "", {})


# --------------------------------------------------------------------------- #
# agent_api: call another agent's REST API with an issued key                  #
# --------------------------------------------------------------------------- #

_AGENT_API_TYPE = "agent_api"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """``(scheme, host, effective port)``, lowercased; None when unparseable."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    return scheme, parts.hostname.lower(), port if port is not None else _DEFAULT_PORTS.get(scheme)


def _agent_api_entry(slot: str) -> dict:
    """The delivered ``agent_api`` entry for ``slot`` (resolved like
    :func:`get_credential`), or :class:`CredentialError`."""
    for candidates in _candidate_groups(slot):
        if not candidates:
            continue
        usable = [e for e in candidates
                  if not e.get("is_placeholder") and not e.get("unavailable_reason")]
        if not usable:
            hint = _unavailable_hint(candidates)
            if hint:
                raise CredentialError(f"credential slot {slot!r} is unavailable: {hint}.")
            break
        entry = usable[0]
        if entry.get("type") != _AGENT_API_TYPE:
            raise CredentialError(
                f"credential slot {slot!r} is of type {entry.get('type')!r}, "
                f"not {_AGENT_API_TYPE!r}."
            )
        data = entry.get("credential_data")
        if not isinstance(data, dict) or not data.get("base_url") or not data.get("token"):
            break
        return entry
    raise CredentialError(
        f"credential slot {slot!r} is not configured. Attach an issued agent API "
        f"key with this slot to the agent."
    )


class AgentApiResponse:
    """A small response: ``.status``, ``.headers``, ``.text``, ``.json()``,
    ``.ok``, ``raise_for_status()``."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str):
        self.status = status
        self.headers = headers
        self.url = url
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def json(self) -> Any:
        return json.loads(self._body.decode("utf-8")) if self._body else None

    def raise_for_status(self) -> None:
        if not self.ok:
            raise CredentialError(
                f"agent API call to {self.url} failed with HTTP {self.status}: "
                f"{self.text[:300]}"
            )


class _OriginPinnedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-attach ``Authorization`` on a same-origin redirect hop, strip it on any
    other (a different scheme, host or port, so https->http too)."""

    def __init__(self, origin: tuple[str, str, int | None] | None, authorization: str):
        super().__init__()
        self._origin = origin
        self._authorization = authorization

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        for name in [n for n in new.headers if n.lower() == "authorization"]:
            del new.headers[name]
        if self._origin is not None and _origin(new.full_url) == self._origin:
            new.add_unredirected_header("Authorization", self._authorization)
        return new


class AgentApiClient:
    """Stdlib client for one ``agent_api`` slot.

    Relative paths join ``base_url``. The token is re-read from the credential
    files on every request, so a rotated key is picked up without a restart.
    ``Authorization`` is sent only to ``base_url``'s exact origin, on every
    redirect hop too.
    """

    def __init__(self, slot: str):
        self.slot = slot
        data = _agent_api_entry(slot)["credential_data"]
        self.base_url = str(data["base_url"]).rstrip("/")
        self.spec_url = data.get("spec_url") or f"{self.base_url}/openapi.json"
        self._origin = _origin(self.base_url)

    def _url(self, path: str, params: dict | None) -> str:
        if path.lower().startswith(("http://", "https://")):
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,  # noqa: A002 - mirrors requests
        data: bytes | str | dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> AgentApiResponse:
        cred = _agent_api_entry(self.slot)["credential_data"]
        if _origin(str(cred["base_url"])) != self._origin:
            # The slot now points at another producer origin: never send its
            # token to the origin this client was built for.
            raise CredentialError(
                f"credential slot {self.slot!r} now points at a different API "
                f"origin; create a new agent_api_session."
            )
        authorization = f"Bearer {cred['token']}"
        url = self._url(path, params)
        body: bytes | None = None
        send_headers = {k: v for k, v in (headers or {}).items()
                        if k.lower() != "authorization"}
        if json is not None:
            body = _json_dumps(json).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        elif data is not None:
            body = data
        req = urllib.request.Request(url, data=body, method=method.upper(), headers=send_headers)
        if self._origin is not None and _origin(url) == self._origin:
            # Unredirected: urllib never copies it to a redirect hop by itself.
            req.add_unredirected_header("Authorization", authorization)
        opener = urllib.request.build_opener(_OriginPinnedRedirects(self._origin, authorization))
        try:
            with opener.open(req, timeout=timeout) as resp:
                return AgentApiResponse(resp.status, dict(resp.headers.items()), resp.read(), resp.geturl())
        except urllib.error.HTTPError as err:
            return AgentApiResponse(err.code, dict(err.headers.items()) if err.headers else {},
                                    err.read() or b"", err.geturl() or url)

    def get(self, path: str, **kwargs: Any) -> AgentApiResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> AgentApiResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> AgentApiResponse:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> AgentApiResponse:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> AgentApiResponse:
        return self.request("DELETE", path, **kwargs)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        resp = self.get(path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post_json(self, path: str, payload: Any = None, **kwargs: Any) -> Any:
        resp = self.post(path, json=payload, **kwargs)
        resp.raise_for_status()
        return resp.json()


_json_dumps = json.dumps


def _requests_session(slot: str) -> Any:
    """An origin-pinned ``requests.Session`` (token fixed at creation)."""
    try:
        import requests
    except ImportError:
        raise CredentialError(
            "agent_api_session(..., requests=True) needs the 'requests' package; "
            "install it or use the default client."
        ) from None
    entry = _agent_api_entry(slot)
    data = entry["credential_data"]
    base_url = str(data["base_url"]).rstrip("/")
    origin = _origin(base_url)
    authorization = f"Bearer {data['token']}"

    class _PinnedSession(requests.Session):
        def request(self, method, url, *args, **kwargs):  # type: ignore[override]
            target = str(url)
            if not target.lower().startswith(("http://", "https://")):
                target = f"{base_url}/{target.lstrip('/')}"
            headers = dict(kwargs.pop("headers", None) or {})
            headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            headers["Authorization"] = authorization if _origin(target) == origin else None
            return super().request(method, target, *args, headers=headers, **kwargs)

        def rebuild_auth(self, prepared_request, response):  # type: ignore[override]
            prepared_request.headers.pop("Authorization", None)
            if origin is not None and _origin(str(prepared_request.url)) == origin:
                prepared_request.headers["Authorization"] = authorization

    session = _PinnedSession()
    session.base_url = base_url  # type: ignore[attr-defined]
    session.spec_url = data.get("spec_url") or f"{base_url}/openapi.json"  # type: ignore[attr-defined]
    return session


def agent_api_session(slot: str, *, requests: bool = False) -> Any:
    """A client for the ``agent_api`` credential in ``slot`` (an issued key).

    Default: a stdlib :class:`AgentApiClient`. ``requests=True``: an
    origin-pinned ``requests.Session`` (token fixed at creation). Raises
    :class:`CredentialError` when the slot is missing, of another type, or
    unavailable (``key_revoked`` / ``key_expired`` / ``key_paused``).
    """
    if requests:
        return _requests_session(slot)
    return AgentApiClient(slot)


def main() -> int:
    """Report which declared slots are configured. Prints names, never values."""
    slots = _slots()
    if not slots:
        print("No credential slots declared in cinna-agent.json.")
        return 0
    for slot in slots:
        name = str(slot.get("name", ""))
        configured = has_credential(name)
        hint = None if configured else unavailable_reason(name)
        state = "configured" if configured else (f"unavailable: {hint}" if hint else "missing")
        print(f"{name}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
