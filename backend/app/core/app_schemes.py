"""Cinna Mobile private-use URI scheme (RFC 8252 §7.1).

The production build uses ``cinna-mobile``; dev/preview/staging builds add a
hyphenated suffix (e.g. ``cinna-mobile-dev``). One owner for the rule, shared by
desktop_auth redirect validation and the credential OAuth return scheme.
"""
import re

MOBILE_APP_SCHEME_PATTERN = r"cinna-mobile(-[a-z0-9]+)*"
MOBILE_RETURN_SCHEME_RE = re.compile(MOBILE_APP_SCHEME_PATTERN)


def is_mobile_return_scheme(value: str | None) -> bool:
    """True when ``value`` is exactly a Cinna Mobile scheme (full match, no ``://``)."""
    if not value:
        return False
    return MOBILE_RETURN_SCHEME_RE.fullmatch(value) is not None
