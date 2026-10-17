# Artifacts - Technical Reference

Business behaviour: [artifacts.md](artifacts.md).

## Data model

Models in `backend/app/models/artifacts/`, services in `backend/app/services/artifacts/`.

| Table | Key fields |
|-------|-----------|
| `artifact` (`artifact.py`) | `id`, `owner_id`, `user_workspace_id`, `title` (200), `description` (2000), `kind` (`site`/`files`), `entry_path`, `visibility` (`private`/`instance`), `current_revision_id` (plain indexed UUID, **no FK**, avoids a circular cascade), `source_kind` (`desktop`/`core_agent`/`cli`/`upload`), `source_label`, `source_key`, source agent/session refs, timestamps |
| `artifact_revision` | `artifact_id` (cascade), `number` (unique per artifact), `size_bytes`, `file_count`, `files` JSON `[{path,size,sha256,mime}]`, `excluded` JSON, `created_by`, `note`, `created_at` |
| `artifact_grant` | `artifact_id`, `user_id`, `granted_by`; unique `(artifact_id, user_id)`; level `view` only |
| `artifact_link` | `artifact_id` (cascade), `token_hash` (SHA-256, unique), `token_prefix` (8 chars), `label`, `expires_at`, `code_encrypted` (Fernet), `failed_attempts`, `pinned_revision_id` (FK, no cascade), `created_by`, `revoked_at` |

API schemas in the same files: `ArtifactPublic`, `ArtifactPublished` (adds `revision_number`, `excluded`), `ArtifactDetailPublic` (revisions; owner also gets `grants`, `links`), `ArtifactViewPublic` (`content_base`, `entry`, `files`, `revision_number`, `expires_at`, `download_url`), `ArtifactsCapabilities` (`version`, `limits`). Request models in `artifact_source.py` (`AgentArtifactPublishRequest`, `ArtifactFromEnvironmentRequest`) forbid extra fields.

## Storage

`ARTIFACT_STORAGE_DIR` (default `/app/data/artifacts`):

```
<artifact_id>/<revision_id>/<relative paths>     served files
<artifact_id>/<revision_id>.zip                  cached revision zip (see ArtifactStorage.revision_zip)
.tmp/<uuid>/                                      ingest scratch, same filesystem; stale ones swept after 1 h
```

`ArtifactStorage.promote` renames a validated scratch folder into place atomically; DB rows are written last. User deletion removes the folders via `user_data_cleanup_service.py`.

## Settings (`backend/app/core/config.py`)

`ARTIFACT_STORAGE_DIR`, `ARTIFACT_MAX_REVISION_BYTES` (50 MiB), `ARTIFACT_MAX_REVISION_FILES` (2000), `ARTIFACT_MAX_FILE_BYTES` (25 MiB), `ARTIFACT_UPLOAD_OVERHEAD_BYTES` (5 MiB multipart allowance), `ARTIFACT_USER_QUOTA_BYTES` (2 GiB), `ARTIFACT_MAX_COMPRESSION_RATIO` (100), `ARTIFACT_VIEW_CAP_TTL_SECONDS` (8 h), `ARTIFACT_LINK_MAX_CODE_ATTEMPTS` (3), `ARTIFACTS_CONTENT_ORIGIN` (optional; defaults to `backend_base_url`, property `artifacts_content_origin`). `frame-ancestors` is the origin of `FRONTEND_HOST`.

## Services

| Module | Role |
|--------|------|
| `artifact_ingest.py` | `ArtifactIngestService.ingest()`: name validation, exclusions, limits, sha256/MIME, kind detection; zip or file-list source |
| `artifact_storage.py` | paths, `promote`, `remove_artifact`, `remove_revision`, `sweep_stale_tmp` |
| `artifact_mime.py` | MIME by extension, HTML detection |
| `artifact_service.py` | `ArtifactService`: create/republish, list, detail, update, delete, delete revision, replace grants, view, download, capabilities. Quota = `SUM(size_bytes)` over the owner's revisions, checked on declared size pre-ingest and again in the commit transaction under `SELECT ... FOR UPDATE` on the owner's `user` row |
| `artifact_access.py` | `ArtifactAccess.can_view/can_manage/owner_valid/link_serves/link_usable`; `agent_publish_policy` (agents create/republish private `core_agent` artifacts only) |
| `artifact_cap.py` | `mint`, `verify`, `content_base`, `zip_url`; `via` = `u:<user_id>` or `l:<link_id>` |
| `artifact_content.py` | `ArtifactContentService.resolve_revision/lookup/resolve/revision_zip`; header builders; in-process LRU manifest cache keyed by `revision_id` (256 entries) |
| `artifact_link_service.py` | `create`, `revoke`, `info`, `open`; token = `secrets.token_urlsafe(32)`; code check under `SELECT ... FOR UPDATE` |
| `artifact_source_service.py` | `publish_from_agent`, `publish_from_environment`: pull from the container via the environment adapter (`fetch_workspace_item_with_meta`); zip response = folder, else single file; env not running = 409 |
| `artifact_errors.py` | `ArtifactError(status, message, detail)`; routes map it to HTTP via `artifact_errors()` |

## Routes

Route modules: `backend/app/api/routes/artifacts.py`, `backend/app/api/routes/artifact_links.py` (public link info/open), `backend/app/api/routes/artifact_content.py`, `backend/app/api/routes/artifact_agent_api.py`. All under `/api/v1`, user JWT (`CurrentUser`) unless noted. Owner-only operations return 404 to non-viewers.

| Route | Purpose |
|-------|---------|
| `GET /artifacts/capabilities` | `{version: 1, limits}` |
| `POST /artifacts` | publish (multipart `archive` xor `files[]`, plus metadata) |
| `POST /artifacts/{id}/revisions` | republish (owner) |
| `GET /artifacts` | list; `scope=mine\|shared_with_me`, `q`, `user_workspace_id`, `source_key`, `skip`, `limit` (max 200) |
| `GET/PATCH/DELETE /artifacts/{id}` | detail / edit title, description, entry, visibility / delete |
| `DELETE /artifacts/{id}/revisions/{number}` | 409 for current or live-pinned |
| `GET /artifacts/{id}/download` | zip (optional `rev`) |
| `POST /artifacts/{id}/view` | mint a cap, returns `ArtifactViewPublic` |
| `PUT /artifacts/{id}/grants` | replace grants (`ArtifactGrantIn {principal, access}`, user principals only) |
| `POST /artifacts/{id}/links`, `DELETE /artifacts/{id}/links/{link_id}` | create (token and code returned once) / revoke |
| `POST /artifacts/from-environment` | owner publishes from an agent workspace (`artifact_agent_api.py`) |
| `POST /agent/artifacts/publish` | agent tool endpoint, env token (`AgentEnvContextDep`) |
| `GET /artifact-links/{token}/info`, `POST /artifact-links/{token}/open` | public, no auth; `open` body `{code}`; 403 carries `attempts_left`, `blocked`; 410 expired |

Content router (`artifact_content.py`, no `/api/v1` prefix, hidden from OpenAPI): `GET|HEAD /a/v/{cap}`, `/a/v/{cap}/{path}` (`?dl=1` for attachment; a directory path gets a 307 to its trailing-slash form), `/a/z/{cap}` (revision zip).

Account CLI (`backend/app/api/routes/cli.py`, account token): `POST /cli/account/artifacts`, `POST /cli/account/artifacts/{id}/revisions`, `GET /cli/account/artifacts/{id}/download`, and `POST /cli/account/artifacts/{id}/links`. They call the same services as the JWT routes.

## Content serving and headers

- `ArtifactContentMiddleware` (`backend/app/core/artifact_content_middleware.py`) is added after `CORSMiddleware` in `backend/app/main.py`, so it is outermost. For `/a/` it answers `OPTIONS` itself, strips credentialed/reflected CORS headers and sets `Access-Control-Allow-Origin: *` with `Expose-Headers: Content-Length, Content-Range, Accept-Ranges`, stamps error security headers on any response missing a CSP, and sets `Cache-Control: no-store` on status >= 400.
- Served files: strict CSP with `sandbox allow-scripts allow-popups ... allow-forms`, `default-src 'none'`, sources scoped to `<content origin>/a/v/<cap>/`, `frame-ancestors <FRONTEND_HOST origin>`; `Cache-Control: private, max-age=3600`; `ETag` = file sha256; `nosniff`; `Referrer-Policy: no-referrer`.
- Error responses: sandbox + `default-src 'none'` only, `no-store`.
- Every `/a/` request: `artifact_cap.verify` (JWT, `aud=artifact_view`), then artifact + revision existence, then `_principal_allowed` (user: `can_view`; link: `link_serves` and pin match). Only the revision manifest is cached.
- `ArtifactUploadLimitMiddleware` (`backend/app/core/artifact_upload_limit_middleware.py`) refuses POST/PUT/PATCH bodies on `/api/v1/(cli/account/)?artifacts` larger than `ARTIFACT_MAX_REVISION_BYTES + ARTIFACT_UPLOAD_OVERHEAD_BYTES` (413), by `Content-Length` and while streaming.

## Proxy and CLI

- `backend/app/services/cli/account_api_proxy_policy.py` denies `POST artifacts/{id}/links` through the generic api-proxy (the CLI must use the dedicated route so the one-time token/code is delivered interactively) and blanks `links[*].code` in the response of `GET artifacts/{id}` (house rule C18).
- `frontend/nginx.conf`: `/a/` block and the 55 MiB body block; see [nginx_setup.md](../../infrastructure/nginx_setup.md).

## Discovery

`/.well-known/cinna-desktop` carries `artifacts: 1` (bumped only on a breaking API change); `GET /artifacts/capabilities` returns the limits.

## Frontend

- Routes: `frontend/src/routes/_layout/artifacts.tsx` (list), `frontend/src/routes/_layout/artifact/$artifactId.tsx` (page), `frontend/src/routes/s/$linkToken.tsx` (public, outside `_layout`). The global 403 handler in `main.tsx` skips `/s/`. Sidebar: `AppSidebar.tsx` / `Main.tsx` with an `activePrefix` so `/artifact/*` keeps the entry lit.
- `frontend/src/components/Artifacts/`: `ArtifactsList`, `artifactColumns`, `ArtifactSegments`, `UploadArtifactDialog` / `UploadSourcePicker` / `PublishResultPanel`, `Page/*` (header, details panel, `RevisionRow`, `AllRevisionsSheet`), `Share/*` (people form via `CatalogPrincipalPicker` with `allowRoles={false}`, links pane, create form, one-time `LinkCreatedPanel`), `PublicLink/PublicLinkScreens.tsx`, `PublishFromWorkspaceDialog` (calls `POST /artifacts/from-environment`; finds the earlier publish through `source_key=<agent_id>:<path>`), `artifactQueries.ts`.
- Viewers (`viewers/*`): site = iframe with `sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals allow-forms"`, never `allow-same-origin`; files = `ArtifactFileTree` plus `FilePreview` by `fileKinds.ts`. Markdown via `MarkdownViewer` (raw HTML off, `urlTransform` rewrites relative links to cap URLs); CSV via `utils/csv.ts` and `@tanstack/react-virtual`; PDF via `pdfjs-dist` 4.10.38 in-page (`isEvalSupported: false`, worker through Vite `?worker`). Code files: `fileKinds.ts` `codeLanguageOf(path)` maps extension to a highlight.js grammar (`.toml`/`.ini` use `ini`; `.tsx`/`.jsx` use the plain TS/JS grammars; `.txt`, `.log`, `.env` and unlisted stay plain); `CodeFileViewer` renders plain text first, then dynamically imports `codeHighlight.ts` (lowlight + 16 grammars, `highlight.js` pinned `~11.11` to match lowlight, lazy chunk like pdf.js) and swaps in coloured spans in the same frame; files over `HIGHLIGHT_LIMIT` (200 KB) or a failed load/highlight stay plain. lowlight's hast tree goes through `hast-util-to-jsx-runtime`, so file text only ever becomes React text nodes (no `innerHTML`, no HTML parsing); token colours come from theme tokens.
- Workspace hook: "Publish as artifact" in `TreeItemRenderer`, `WorkspaceTabContent`, `EnvironmentPanel` (agent owner only).
- E2E: `frontend/tests/e2e/artifacts.e2e.mjs` (`npm run test:artifacts:e2e`, against the nginx build on :5173 after `docker compose build frontend`): sandbox probe, viewers incl. PDF, sql.js dashboard, public link and code flow.

## Agent verb

- Kit contract 1.12.0: `docs/local_agent_kit/protocols/artifacts.schema.json` (one tool, `artifacts_publish`), `docs/local_agent_kit/skills/cinna-artifacts/SKILL.md`, `docs/local_agent_kit/templates/artifact-sqlite-dashboard/`. The platform skill is projected into environments through `platform_protocol_files.py` and `skill_manifest`.
- Env tool: `cinna_artifacts_protocol.py` (`CinnaArtifactsClient`) calls `POST /api/v1/agent/artifacts/publish` with the env token and `X-Agent-Env-Id` (120 s timeout). Claude Code: building mode registers `cinna` with artifacts only, conversation mode with tasks and artifacts (`cinna_toolsets_for_mode`). The OpenCode bridge serves both in both modes. An older core without the schema exposes task tools only. The name is in `tool_name_registry` pre-approved and `message_service` `PRE_ALLOWED_TOOLS`.
- Placeholder arguments: before posting, the client drops an `artifact_id` that is blank, not a UUID, or the nil UUID, and an `entry` that is not an `.html`/`.htm` file inside the published folder (an entry written from the workspace root, `artifacts/site/index.html`, is made folder-relative). A 404 for a sent `artifact_id` tells the model to omit it. Why: OpenCode 1.14 sends tools without `"strict": false`, OpenAI's Responses API then treats every property as required, and gpt-5.4-mini / gpt-5.6-luna filled both fields with placeholders (`" "`, `"/"`, the nil UUID, the `.md` itself as `entry`) and looped ~30 times on the 422s. OpenCode 1.18+ sends `"strict": false`. Tests: `tests/unit/test_cinna_artifacts_client.py`.
- Backend: `artifact_agent_api.py` (agent route) and `POST /artifacts/from-environment` share `artifact_source_service.py`. The single publish gate is `artifact_access.agent_publish_policy()` (`may_republish`: same owner, `core_agent`, same agent); request models forbid extras. `source_key = "<agent_id>:<path>"`, e.g. `<agent_id>:artifacts/<name>`, so republishing the same path updates the same artifact.
- Where agents build: `artifacts/<name>/` in the workspace (kit layout role `artifacts`, contract 1.13.0). It lives in the workspace, not a bind mount like `app-data/`. `workspace_classification.BUNDLE_EXCLUDED_TOPLEVEL` contains `artifacts` (no bundle publish snapshot, install seed, git source, or delete on bundle update); `ENV_MIGRATION_EXTRA` carries it across env switch; `library/push_policy.py` refuses team-library pushes with `MSG_ARTIFACTS`; `prompt_generator.py` emits the "Artifacts location" line. `app-data/` remains excluded from publish.
- Pull: environment `GET /workspace/download/{path}?artifact=1` runs `create_artifact_zip` (skips dotfiles, `__pycache__`, all symlinks; `O_NOFOLLOW` reads); skipped names return in `X-Cinna-Skipped` and are merged into `excluded`. Normal downloads and attachments are unchanged.
