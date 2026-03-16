"""
Sync documentation and auto-generated API reference into the General
Assistant environment template's knowledge/platform/ directory.

Two data sources:
  1. docs/application/ and docs/agents/ — business-logic docs (excluding *_tech* files)
  2. frontend/openapi.json — auto-generated REST API reference grouped by tag

Usage:
    python3 .runnerkit/scripts/sync_ga_knowledge.py

Run from the project root whenever you update documentation or API routes.
Prerequisite: run `make gen-client` first so openapi.json is up to date.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DOC_SOURCES = {
    "application": PROJECT_ROOT / "docs" / "application",
    "agents": PROJECT_ROOT / "docs" / "agents",
}
DOCS_README = PROJECT_ROOT / "docs" / "README.md"
OPENAPI_PATH = PROJECT_ROOT / "frontend" / "openapi.json"
TARGET = (
    PROJECT_ROOT
    / "backend"
    / "app"
    / "env-templates"
    / "general-assistant-env"
    / "app"
    / "workspace"
    / "knowledge"
    / "platform"
)

# Tags to skip — internal/irrelevant for the General Assistant
SKIP_TAGS = {
    "login", "oauth", "private", "utils", "items",
    "mcp-oauth", "mcp-upload", "mcp-consent",
    "webapp-public", "webapp-shares", "shared-workspace",
    "security-events",
}


# ---------------------------------------------------------------------------
# API reference generation from OpenAPI spec
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


def _response_type(spec: dict, responses: dict) -> str:
    """Extract the 200-level response type name."""
    for code in ("200", "201"):
        resp = responses.get(code, {})
        content = resp.get("content", {})
        for ct, info in content.items():
            schema = info.get("schema", {})
            if "$ref" in schema:
                return schema["$ref"].rsplit("/", 1)[-1]
            if schema.get("type") == "object":
                return "object"
    return ""


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
                for ct, schema_info in content.items():
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
            resp_type = _response_type(spec, op.get("responses", {}))
            if resp_type:
                lines.append(f"**Response:** `{resp_type}`")
                lines.append("")

            lines.append("---")
            lines.append("")

        result[tag] = "\n".join(lines)

    return result


def _tag_title(tag: str) -> str:
    """Convert tag slug to title: 'mail-servers' → 'Mail Servers'."""
    return tag.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Documentation sync
# ---------------------------------------------------------------------------

def sync_docs() -> int:
    """Copy business-logic docs (excluding _tech files). Returns file count."""
    total = 0
    for name, src in DOC_SOURCES.items():
        if not src.is_dir():
            raise SystemExit(f"ERROR: docs/{name}/ not found at {src}")
        count = 0
        for md_file in src.rglob("*.md"):
            if "_tech" in md_file.name:
                continue
            rel = md_file.relative_to(src)
            dest = TARGET / name / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(md_file, dest)
            count += 1
        print(f"  Copied {count} doc files → platform/{name}/")
        total += count

    if DOCS_README.is_file():
        shutil.copy2(DOCS_README, TARGET / "README.md")
        total += 1
        print(f"  Copied docs/README.md → platform/README.md")

    return total


def sync_api_reference() -> int:
    """Generate API reference from OpenAPI spec. Returns file count."""
    if not OPENAPI_PATH.is_file():
        print(f"  WARNING: {OPENAPI_PATH.relative_to(PROJECT_ROOT)} not found — skipping API reference")
        print(f"           Run `make gen-client` first to generate the OpenAPI spec")
        return 0

    with open(OPENAPI_PATH) as f:
        spec = json.load(f)

    api_dir = TARGET / "api_reference"
    api_dir.mkdir(parents=True, exist_ok=True)

    references = generate_api_reference(spec)

    # Write per-tag files
    for tag, content in references.items():
        filename = tag.replace("-", "_") + ".md"
        (api_dir / filename).write_text(content)

    # Write index
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
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            for tag in op.get("tags", []):
                if tag not in SKIP_TAGS:
                    by_tag.setdefault(tag, []).append(1)

    for tag in sorted(references):
        filename = tag.replace("-", "_") + ".md"
        count = len(by_tag.get(tag, []))
        index_lines.append(f"| {_tag_title(tag)} | [{filename}](./{filename}) | {count} |")

    (api_dir / "README.md").write_text("\n".join(index_lines) + "\n")

    total = len(references) + 1  # +1 for README
    print(f"  Generated {len(references)} API reference files → platform/api_reference/")
    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Syncing GA knowledge...")
    print(f"  Target: {TARGET.relative_to(PROJECT_ROOT)}")
    print()

    # Clear and recreate
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)

    print("[1/2] Copying feature documentation (excluding _tech files)...")
    doc_count = sync_docs()

    print()
    print("[2/2] Generating API reference from OpenAPI spec...")
    api_count = sync_api_reference()

    print()
    print(f"Sync complete. Total files: {doc_count + api_count}")


if __name__ == "__main__":
    main()
