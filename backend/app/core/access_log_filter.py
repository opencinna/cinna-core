"""Keep content URLs out of the access log and Sentry (D-C3).

A component page may carry data in its path or query (``/d/v/<cap>/<path>?…``),
and the path holds the frame cap itself, so no ``/d/`` request is ever logged.
nginx does the same with ``access_log off`` on its ``/d/`` location; this filter
covers uvicorn's own access log, whatever sits in front of it.
"""
import logging
import re
from urllib.parse import urlsplit

ACCESS_LOGGER = "uvicorn.access"
UNLOGGED_PREFIXES = ("/d/",)


def _request_path(record: logging.LogRecord) -> str | None:
    """The request path of a uvicorn access record (``args`` = client, method, path, version, status)."""
    args = record.args
    if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
        return args[2]
    return None


class DropUnloggedPathsFilter(logging.Filter):
    """Drops access records whose request path starts with an unlogged prefix."""

    def filter(self, record: logging.LogRecord) -> bool:
        path = _request_path(record)
        return path is None or not path.startswith(UNLOGGED_PREFIXES)


def install_access_log_filter() -> None:
    """Attach the filter to ``uvicorn.access`` once (idempotent across lifespans)."""
    access_logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, DropUnloggedPathsFilter) for f in access_logger.filters):
        access_logger.addFilter(DropUnloggedPathsFilter())


# ── Sentry ───────────────────────────────────────────────────────────
#
# The Sentry SDK reports ``request.url``, the query string, the transaction name
# and breadcrumbs. Content URLs (``/a/`` artifacts, ``/d/`` dashboard frames)
# carry a capability and possibly page data, so they never reach it: content
# transactions are dropped, and error events are scrubbed.

CONTENT_PREFIXES = ("/a/", "/d/")
FILTERED = "[Filtered]"
# A capability URL anywhere inside free text (a breadcrumb message, say).
_CAP_URL = re.compile(r"/(?:a/[vz]|d/v)/[^\s\"'<>]*")


def _content_prefix(value: object) -> str | None:
    """The content prefix ``value`` (a URL or a path) is under, or None."""
    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value)
    path = parts.path if parts.scheme or parts.netloc else value.split("?", 1)[0]
    return next((p for p in CONTENT_PREFIXES if path.startswith(p)), None)


def _scrub_url(value: object) -> object:
    prefix = _content_prefix(value)
    if prefix is None:
        return _CAP_URL.sub(lambda m: m.group(0)[:3] + FILTERED, value) if isinstance(value, str) else value
    parts = urlsplit(str(value))
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    return f"{origin}{prefix}{FILTERED}"


def _is_content_event(event: dict) -> bool:
    request = event.get("request")
    url = request.get("url") if isinstance(request, dict) else None
    return _content_prefix(url) is not None or _content_prefix(event.get("transaction")) is not None


def _scrub_breadcrumbs(event: dict) -> None:
    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    for crumb in values if isinstance(values, list) else []:
        if not isinstance(crumb, dict):
            continue
        if "message" in crumb:
            crumb["message"] = _scrub_url(crumb["message"])
        data = crumb.get("data")
        if not isinstance(data, dict):
            continue
        if _content_prefix(data.get("url")) is not None:
            # The query and fragment of a content URL are page data too.
            data.pop("http.query", None)
            data.pop("http.fragment", None)
        for key in ("url", "to", "from"):
            if key in data:
                data[key] = _scrub_url(data[key])


def _drop_frame_vars(event: dict) -> None:
    """Remove local variables (a handler's ``cap``/``path``) from every stack frame."""
    for section in ("exception", "threads"):
        container = event.get(section)
        values = container.get("values") if isinstance(container, dict) else None
        for value in values if isinstance(values, list) else []:
            stacktrace = value.get("stacktrace") if isinstance(value, dict) else None
            frames = stacktrace.get("frames") if isinstance(stacktrace, dict) else None
            for frame in frames if isinstance(frames, list) else []:
                if isinstance(frame, dict):
                    frame.pop("vars", None)


def sentry_before_send(event: dict, hint: dict) -> dict:
    """Scrub content URLs from an error event (request, transaction, frame vars, breadcrumbs)."""
    if _is_content_event(event):
        _drop_frame_vars(event)
        request = event.get("request")
        if isinstance(request, dict):
            request["url"] = _scrub_url(request.get("url"))
            request.pop("query_string", None)
        if "transaction" in event:
            event["transaction"] = _scrub_url(event["transaction"])
    _scrub_breadcrumbs(event)
    return event


def sentry_before_send_transaction(event: dict, hint: dict) -> dict | None:
    """Drop content-request transactions; scrub breadcrumbs on the rest."""
    if _is_content_event(event):
        return None
    _scrub_breadcrumbs(event)
    return event
