---
feature: docs_tooling
domain: development
one_liner: "Frontmatter-driven docs index: generated README registry for humans, summary/search/impact/graph commands for agents, and checks that keep docs tied to code."
primary_label: reference
---
# Docs Tooling

The documentation tree has one source of truth for its index — a small YAML frontmatter block at
the top of each feature's primary doc — and two views generated from it: the Feature Registry
block in `docs/README.md` for people reading on GitHub, and compact, searchable and graph views
for LLM agents. A second script keeps docs logically tied to the code they describe.

Scripts live in `.cinna-core-kit/scripts/`:

| Script | Job |
|--------|-----|
| `docs_index.py` | Index views, impact analysis, feature graph, consistency checks |
| `check_docs_references.py` | Every `backend/`, `frontend/`, `docs/` path a doc names must exist |
| `sync_platform_knowledge.py` | Mirrors docs into the platform-knowledge env template |

`make check-docs` runs the first two; `make docs-registry` regenerates the README block.

## Frontmatter

```yaml
---
feature: server_channels
domain: application                       # or a list: [application, knowledge]
one_liner: "Admin-configured server-wide inbound channels routing outside senders to agents."
docs:                                     # every other doc of the feature, label -> path
  tech: server_channels_tech.md           # relative to this doc's directory (or docs/-rooted)
  debug monitor tech: channel_debug_monitor_tech.md
primary_label: business logic             # optional; label of THIS doc in the registry
affects: [routing_tuning]                 # optional, directional, sparse
covers: [backend/app/services/server_channels/**]   # optional override globs
---
```

Rules:

- `one_liner` is one plain sentence, at most 200 characters, no markdown, no route paths, no
  release-note phrasing. The registry cannot grow into release notes again because the check
  refuses longer values.
- Every doc of a feature is listed in exactly one feature's `docs:` mapping (or is the primary).
  The check warns about docs that belong to no feature.
- `affects` is the only authored relation. It means "when this feature changes, review that
  feature's docs too". Add it only when the doc itself states the dependency. `related` is never
  authored — it is derived from the links docs already carry.
- `covers` is normally unnecessary: the paths a doc names in backticks or links already tie it to
  code. Use it only for a doc that describes code it never names.
- Change history goes into a `## Changelog` section at the end of the business doc, never into
  the one-liner or the registry.

GitHub renders the frontmatter as a table at the top of the file, so it stays readable.

## Commands

```
python3 .cinna-core-kit/scripts/docs_index.py summary [--domain D]
python3 .cinna-core-kit/scripts/docs_index.py search TERM [TERM...] [--feature F] [--domain D] [--limit N]
python3 .cinna-core-kit/scripts/docs_index.py impact [--files F...] [--base REF]
python3 .cinna-core-kit/scripts/docs_index.py path FROM TO
python3 .cinna-core-kit/scripts/docs_index.py graph [--feature F]
python3 .cinna-core-kit/scripts/docs_index.py stale [--feature F]
python3 .cinna-core-kit/scripts/docs_index.py registry [--check]
python3 .cinna-core-kit/scripts/docs_index.py check [--verbose]
```

- **summary** — one line per feature per domain, in Domain Map order. This is what "read core"
  loads instead of the full README.
- **search** — case-insensitive text search across the docs tree (plans excluded), grouped by
  file with the owning feature and nearest heading.
- **impact** — the docs that name any file in the current diff (working tree vs HEAD plus
  untracked; or `--files`, or `--base REF`), then one hop along `affects`. The documenter agent
  treats the result as its minimum review set; the code-review command reports docs it lists
  that the diff did not touch.
- **path / graph** — the feature graph: undirected `related` edges derived from doc links, plus
  directed `affects` edges. `path` prints the shortest chain between two features.
- **stale** — docs whose referenced code has commits newer than the doc's own last commit.
  Advisory; useful for periodic audits, not part of `check`.
- **registry** — rewrites the block between `<!-- registry:start -->` and `<!-- registry:end -->`
  in `docs/README.md`. Domain order and the Domain Map table stay hand-authored. `--check`
  only reports staleness.
- **check** — fails on: frontmatter that does not parse or claims a doc twice; missing, `TODO`
  or over-long one-liners, unknown domains, missing docs, unresolved `affects`; a stale registry
  block; service or route modules no doc names (allowlist:
  `.cinna-core-kit/docs_index_allowlist.txt`); `METHOD /api/v1/...` citations that are not in
  `frontend/openapi.json`; tech docs that name no `backend/` or `frontend/` path. Warns on docs
  registered to no feature.

## How docs stay connected to code

| Direction | Mechanism |
|-----------|-----------|
| doc → code path exists | `check_docs_references.py` |
| doc → route exists | `docs_index.py check` (route citations vs OpenAPI spec) |
| code → some doc names it | `docs_index.py check` (orphan modules under services and routes) |
| change → docs to review | `docs_index.py impact` (path references, `covers`, `affects`) |
| doc older than its code | `docs_index.py stale` (advisory) |
| tech doc anchored at all | `docs_index.py check` (at least one code path) |

Prose truth is not machine-checked. The feature-documenter agent verifies claims against code;
these checks give it deterministic input instead of guesswork.

## Adding a feature

1. Write `docs/{domain}/{feature}/{feature}.md` with the frontmatter block, and the tech doc.
2. Run `make docs-registry`, then `make check-docs`.
3. Do not edit the registry block in `docs/README.md`; do not add feature counts to the Domain Map.
