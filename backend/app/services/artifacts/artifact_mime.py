"""Extension → MIME type for artifact files.

The type comes from the extension alone and is never sniffed: the content
route serves exactly what this table says (with ``nosniff``), so a file cannot
upgrade itself to HTML by its bytes. Text types carry ``charset=utf-8``.
Unknown extensions are ``application/octet-stream``.
"""
import posixpath

DEFAULT_MIME = "application/octet-stream"

_TEXT_MIME: dict[str, str] = {
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "json": "application/json",
    "map": "application/json",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "log": "text/plain",
    "svg": "image/svg+xml",
    "xml": "application/xml",
    "yaml": "application/yaml",
    "yml": "application/yaml",
}

_BINARY_MIME: dict[str, str] = {
    "wasm": "application/wasm",
    "sqlite": "application/vnd.sqlite3",
    "sqlite3": "application/vnd.sqlite3",
    "db": "application/vnd.sqlite3",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "ico": "image/x-icon",
    "pdf": "application/pdf",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "zip": "application/zip",
}

MIME_BY_EXTENSION: dict[str, str] = {
    **{ext: f"{mime}; charset=utf-8" for ext, mime in _TEXT_MIME.items()},
    **_BINARY_MIME,
}


def mime_for_path(path: str) -> str:
    """MIME type for a relative artifact path, by its (case-insensitive) extension."""
    _, ext = posixpath.splitext(posixpath.basename(path))
    return MIME_BY_EXTENSION.get(ext[1:].lower(), DEFAULT_MIME) if ext else DEFAULT_MIME


def is_html_path(path: str) -> bool:
    return posixpath.splitext(path)[1].lower() in (".html", ".htm")
