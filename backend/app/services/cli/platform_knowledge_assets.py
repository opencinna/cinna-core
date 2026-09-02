"""
Shared platform knowledge assets.

This module is the single home for two pieces of platform self-knowledge
generation that are consumed by two callers:

  1. ``.cinna-core-kit/scripts/sync_platform_knowledge.py`` — the repo-root sync
     script that snapshots ``docs/`` + the generated API reference into the
     ``platform-knowledge-env`` template at build/dev time (it has access to
     ``docs/`` and ``frontend/openapi.json``, which the backend container does
     NOT).
  2. ``app.services.cli.context_package_service.ContextPackageService`` — the
     account-CLI context-package endpoint, which assembles a tarball from the
     *already-snapshotted* template assets at request time (the only platform
     knowledge available inside the running backend container).

Keeping the API-reference generation and the tag-skip policy here means the two
callers share one implementation instead of duplicating it.
"""

from __future__ import annotations

from pathlib import Path

# NOTE: ``app.core.config`` is imported lazily inside the path helpers below so
# that the pure API-reference generation functions (``generate_api_reference``,
# ``api_reference_index``, …) can be imported by the repo-root
# ``sync_platform_knowledge.py`` script without triggering backend Settings
# validation (which needs a populated ``.env`` / database config).

# ---------------------------------------------------------------------------
# Snapshot locations (resolved relative to the env-templates dir)
# ---------------------------------------------------------------------------
#
# The ``platform-knowledge-env`` template ships a committed, pre-synced snapshot
# of the platform docs + generated API reference + example scripts. This is the
# ONLY copy of that knowledge present inside the backend container at runtime
# (``docs/`` and ``frontend/openapi.json`` live at the repo root and are not
# copied into the image), so the context-package endpoint reads from here.

KNOWLEDGE_TEMPLATE_NAME = "platform-knowledge-env"

# ``knowledge/local-kit/`` — the Local Agent Kit, synced from ``docs/local_agent_kit/``.
LOCAL_KIT_SUBDIR = "local-kit"


def _knowledge_workspace_dir() -> Path:
    """Absolute path to the knowledge template's container workspace root."""
    from app.core.config import settings

    return (
        Path(settings.ENV_TEMPLATES_DIR)
        / KNOWLEDGE_TEMPLATE_NAME
        / "app"
        / "workspace"
    )


def platform_knowledge_dir() -> Path:
    """``knowledge/platform/`` — synced docs + generated ``api_reference/``."""
    return _knowledge_workspace_dir() / "knowledge" / "platform"


def example_scripts_dir() -> Path:
    """``scripts/examples/`` — working API-script patterns."""
    return _knowledge_workspace_dir() / "scripts" / "examples"


def guides_dir() -> Path:
    """``knowledge/guides/`` — hand-authored worked playbooks.

    A sibling of ``knowledge/platform/`` that the docs sync
    (``sync_platform_knowledge.py``) deliberately does NOT touch — its ``rmtree``
    target is ``knowledge/platform/`` only — so playbooks committed here survive
    every knowledge re-sync.
    """
    return _knowledge_workspace_dir() / "knowledge" / "guides"


def local_kit_dir() -> Path:
    """``knowledge/local-kit/`` — the Local Agent Kit snapshot.

    Unlike ``knowledge/guides/`` (hand-authored in place), this tree is a
    *synced snapshot*: ``sync_platform_knowledge.py`` mirrors
    ``docs/local_agent_kit/`` into it verbatim (step 3, clearing only this
    subtree). ``docs/`` is not shipped in the backend image, so this is the only
    copy of the kit available at runtime — the public ``/agent-start`` surface and the
    account context package both read it from here.
    """
    return _knowledge_workspace_dir() / "knowledge" / LOCAL_KIT_SUBDIR


def snapshot_cache_key(*dirs: Path) -> str:
    """
    Cheap cache key derived from the newest mtime AND file count across ``dirs``.

    A redeploy that ships a freshly-synced snapshot bumps file mtimes, which
    changes the key and invalidates any cached build automatically. The file
    count is folded in so a pure deletion (which leaves the max mtime unchanged)
    still invalidates the cache — belt-and-suspenders, since the sync script
    rewrites whole trees anyway. Missing directories contribute nothing, so a
    snapshot source that is absent in one deployment does not break the key.

    Shared by every consumer that memoizes work derived from the snapshot
    (``ContextPackageService``, ``LocalAgentKitService``) so they invalidate on
    exactly the same signal.
    """
    newest = 0.0
    count = 0
    for root in dirs:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file():
                count += 1
                mtime = p.stat().st_mtime
                if mtime > newest:
                    newest = mtime
    return f"{newest:.6f}:{count}"


# Tags to skip — internal/irrelevant for orchestration knowledge.
SKIP_TAGS = {
    "login", "oauth", "private", "utils", "items",
    "mcp-oauth", "mcp-upload", "mcp-consent",
    "webapp-public", "webapp-shares", "shared-workspace",
    "security-events",
}


# ---------------------------------------------------------------------------
# API reference generation from an OpenAPI spec
# ---------------------------------------------------------------------------

def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a $ref pointer like '#/components/schemas/Foo'."""
    parts = ref.lstrip("#/").split("/")
    node = spec
    for p in parts:
        node = node.get(p, {})
    return node


def _schema_summary(spec: dict, schema: dict, depth: int = 0) -> str:
    """Return a compact summary of a JSON schema's fields."""
    if "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])

    # anyOf / oneOf — pick first non-null
    for key in ("anyOf", "oneOf"):
        if key in schema:
            variants = [v for v in schema[key] if v.get("type") != "null"]
            if variants:
                return _schema_summary(spec, variants[0], depth)
            return "any"

    if schema.get("type") == "array":
        items = schema.get("items", {})
        inner = _schema_summary(spec, items, depth)
        return f"{inner}[]"

    if schema.get("type") == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not props:
            return "object"
        if depth > 0:
            return "object"
        lines = []
        for name, prop in props.items():
            ptype = _field_type(spec, prop)
            req = " (required)" if name in required else ""
            desc = prop.get("description", "")
            desc_str = f" — {desc}" if desc else ""
            lines.append(f"  - `{name}`: {ptype}{req}{desc_str}")
        return "\n".join(lines)

    return schema.get("type", schema.get("format", "any"))


def _field_type(spec: dict, schema: dict) -> str:
    """Return a short type string for a schema field."""
    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        return ref_name

    for key in ("anyOf", "oneOf"):
        if key in schema:
            types = []
            nullable = False
            for v in schema[key]:
                if v.get("type") == "null":
                    nullable = True
                elif "$ref" in v:
                    types.append(v["$ref"].rsplit("/", 1)[-1])
                else:
                    types.append(v.get("type", "any"))
            result = " | ".join(types) if types else "any"
            if nullable:
                result += " | null"
            return result

    if schema.get("type") == "array":
        items = schema.get("items", {})
        inner = _field_type(spec, items)
        return f"{inner}[]"

    base = schema.get("type", "any")
    fmt = schema.get("format")
    if fmt == "uuid":
        return "uuid"
    if fmt == "date-time":
        return "datetime"
    if fmt == "binary":
        return "binary"
    enum = schema.get("enum")
    if enum:
        return " | ".join(f'"{v}"' for v in enum)
    return base


def _response_type(responses: dict) -> str:
    """Extract the 200-level response type name."""
    for code in ("200", "201"):
        resp = responses.get(code, {})
        content = resp.get("content", {})
        for info in content.values():
            schema = info.get("schema", {})
            if "$ref" in schema:
                return schema["$ref"].rsplit("/", 1)[-1]
            if schema.get("type") == "object":
                return "object"
    return ""


def _tag_title(tag: str) -> str:
    """Convert tag slug to title: 'mail-servers' → 'Mail Servers'."""
    return tag.replace("-", " ").replace("_", " ").title()


def generate_api_reference(spec: dict) -> dict[str, str]:
    """Generate markdown content per OpenAPI tag. Returns {tag: markdown}."""
    # Group endpoints by tag
    by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            tags = op.get("tags", ["untagged"])
            for tag in tags:
                if tag in SKIP_TAGS:
                    continue
                by_tag.setdefault(tag, []).append((method.upper(), path, op))

    result: dict[str, str] = {}
    for tag in sorted(by_tag):
        lines = [f"# {_tag_title(tag)} — API Reference", ""]
        lines.append(f"Auto-generated from OpenAPI spec. Tag: `{tag}`")
        lines.append("")

        for method, path, op in by_tag[tag]:
            summary = op.get("summary", "")
            lines.append(f"## {method} `{path}`")
            if summary:
                lines.append(f"**{summary}**")
            lines.append("")

            # Parameters
            params = op.get("parameters", [])
            path_params = [p for p in params if p.get("in") == "path"]
            query_params = [p for p in params if p.get("in") == "query"]

            if path_params:
                lines.append("**Path parameters:**")
                for p in path_params:
                    ptype = _field_type(spec, p.get("schema", {}))
                    lines.append(f"- `{p['name']}`: {ptype}")
                lines.append("")

            if query_params:
                lines.append("**Query parameters:**")
                for p in query_params:
                    ptype = _field_type(spec, p.get("schema", {}))
                    req = " (required)" if p.get("required") else ""
                    default = p.get("schema", {}).get("default")
                    default_str = f", default: `{default}`" if default is not None else ""
                    lines.append(f"- `{p['name']}`: {ptype}{req}{default_str}")
                lines.append("")

            # Request body
            rb = op.get("requestBody", {})
            if rb:
                content = rb.get("content", {})
                for schema_info in content.values():
                    schema = schema_info.get("schema", {})
                    if "$ref" in schema:
                        ref_name = schema["$ref"].rsplit("/", 1)[-1]
                        resolved = _resolve_ref(spec, schema["$ref"])
                        lines.append(f"**Request body** (`{ref_name}`):")
                        body_summary = _schema_summary(spec, resolved)
                        lines.append(body_summary)
                    elif schema.get("type") == "object":
                        lines.append("**Request body:**")
                        body_summary = _schema_summary(spec, schema)
                        lines.append(body_summary)
                    break  # first content type only
                lines.append("")

            # Response
            resp_type = _response_type(op.get("responses", {}))
            if resp_type:
                lines.append(f"**Response:** `{resp_type}`")
                lines.append("")

            lines.append("---")
            lines.append("")

        result[tag] = "\n".join(lines)

    return result


def api_reference_index(spec: dict, references: dict[str, str]) -> str:
    """Build the API-reference ``README.md`` index for the given references."""
    index_lines = [
        "# Platform REST API Reference",
        "",
        "Auto-generated from the backend OpenAPI specification.",
        "Each file below documents one API domain.",
        "",
        "| Domain | File | Endpoints |",
        "|--------|------|-----------|",
    ]
    by_tag: dict[str, list] = {}
    for methods in spec.get("paths", {}).values():
        for op in methods.values():
            if not isinstance(op, dict):
                continue
            for tag in op.get("tags", []):
                if tag not in SKIP_TAGS:
                    by_tag.setdefault(tag, []).append(1)

    for tag in sorted(references):
        filename = tag.replace("-", "_") + ".md"
        count = len(by_tag.get(tag, []))
        index_lines.append(f"| {_tag_title(tag)} | [{filename}](./{filename}) | {count} |")

    return "\n".join(index_lines) + "\n"
