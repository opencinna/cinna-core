# Knowledge Sources — Article Preview, Export & Table Overflow Fix

**Status:** Draft plan (ready for manager)
**Scope:** Two UX fixes to the existing admin-only Knowledge Sources feature. No new tables, no migration, no new permission model.

---

## 0. Grounding (verified current state — docs are stale)

- **Routes** `backend/app/api/routes/knowledge_sources.py` — ALL endpoints gated by
  `SuperUser = Annotated[User, Depends(get_current_active_superuser)]`. No `CurrentUser`, no custom read dep.
- **Service** `backend/app/services/knowledge/knowledge_source_service.py`:
  - `get_source_by_id` (~L155): `source = session.get(...); if not source or source.user_id != user_id: return None`
  - `get_source_articles` (~L723): same strict-owner gate, then lists by `git_repo_id`.
  - `get_discoverable_sources` (~L758): the ONLY non-owner read path — returns sources where
    `public_discovery == True AND user_id != current_user AND status == SourceStatus.connected` (metadata only).
  - `get_article_count` (~L700): article count helper.
- **Models** `backend/app/models/knowledge/knowledge.py`:
  - `KnowledgeArticle` (table, ~L168) has `content`, `content_hash`, `commit_hash`, `file_path`.
  - `KnowledgeArticlePublic` (~L189) deliberately OMITS `content`.
  - `KnowledgeArticleDetail(KnowledgeArticlePublic)` (~L204) ADDS `content` + `commit_hash` — **currently dead/unused**. Reuse it.
- **Frontend**:
  - `frontend/src/components/KnowledgeSources/KnowledgeSourceArticlesTab.tsx` — renders bare `<Table>`, description cell `text-sm text-muted-foreground line-clamp-2`. Uses `KnowledgeSourcesService.listKnowledgeArticles`.
  - `frontend/src/routes/_layout/knowledge-source/$sourceId.tsx` — detail page, header has a `DropdownMenu` (Edit / Delete) — natural home for an Export item.
  - `frontend/src/components/ui/table.tsx` — **THE OVERFLOW CULPRIT**: `Table` wraps `<table>` in `<div class="relative w-full overflow-x-auto rounded-lg border">`, and `TableHead`/`TableCell` default to `whitespace-nowrap`. Long descriptions are forced single-line → the wrapper scrolls.
  - Markdown renderers: `frontend/src/components/Chat/MarkdownRenderer.tsx` (`{content, className}`) and `frontend/src/components/Environment/MarkdownViewer.tsx` (`{content, className}`, prose styling, full-document layout). **MarkdownViewer is the better fit** for full-document preview (prose-slate, overflow-auto, code-block handling).
  - SDK service `KnowledgeSourcesService` has `listKnowledgeArticles` only — no `getArticle`, no `export`.
- **Tests** `backend/tests/api/knowledge_sources/test_knowledge_sources.py` — scenario-based, API-only, superuser headers, git/embedding work patched out. Utility helpers in `backend/tests/utils/knowledge_source.py`.

---

## 1. Decisions (explicit — flagged for manager)

### Decision A — Route auth posture: KEEP `SuperUser` on the routes (DO NOT relax to `CurrentUser`)
The entire Knowledge Sources UI is admin-only (route comment L4-7, every sibling route is `SuperUser`). There is **no evidence non-superusers reach this UI**. The faithful, lowest-risk choice:

- **Keep the new routes `SuperUser`-gated** (identical to siblings).
- **Broaden only the SERVICE-level authorization** from strict-owner (`source.user_id == user_id`) to **owner OR public-discoverable read access** (the same boundary that already governs `get_source_by_id` + `get_discoverable_sources`).

This means: a superuser who is NOT the owner but who can *see* a source because it is `public_discovery == True` (enabled + connected) can now also preview an article and export the source. That mirrors the existing "discoverable" read boundary exactly, without inventing sharing.

> **Open decision for manager (A1):** the task brief says "any user who can SEE a knowledge source (because it was shared with them — NOT necessarily owner/admin)". There is *no sharing mechanism* in the backend; the only cross-user visibility is `public_discovery`. This plan interprets "shared with them" as **"publicly discoverable"** and keeps the route superuser-gated. If the manager wants genuine non-superuser access, that is a **separate, larger feature** (new sharing table + relaxed route deps) and is explicitly OUT OF SCOPE here. Confirm the public-discovery interpretation.

### Decision B — Read-access boundary helper (single source of truth)
Add ONE private helper in the service that returns the ORM `AIKnowledgeGitRepo` if the requesting user may read it, else `None`:

```python
def _get_source_for_read(
    *, session: Session, source_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[AIKnowledgeGitRepo]:
    """Return the source if the user may READ it, else None.

    Read access == owner OR (public_discovery AND enabled AND connected).
    Mirrors the boundary already implied by get_source_by_id + get_discoverable_sources.
    """
    source = session.get(AIKnowledgeGitRepo, source_id)
    if not source:
        return None
    if source.user_id == user_id:
        return source
    if (
        source.public_discovery
        and source.is_enabled
        and source.status == SourceStatus.connected
    ):
        return source
    return None
```

Both new service methods (article content + export) use this helper. Existing methods are **left untouched** (no behavior change to current endpoints — keeps the diff scoped and avoids regressions in the discovery/ownership tests).

> Note the `enabled` qualifier: discoverable list already requires `status == connected`; we add `is_enabled` to match "you can only read a source whose articles are live". Owner path ignores enabled/status (owner can always read their own — consistent with current `get_source_by_id`).

### Decision C — Export format: **single concatenated Markdown document** (`text/markdown` download)
Simplest faithful option, directly useful (articles ARE markdown). One document, articles separated by a header block per article. Avoids zip/streaming complexity and binary handling on the frontend. JSON-with-content was the alternative; rejected as less directly consumable and redundant once per-article content endpoint exists.

> **Open decision for manager (C1):** confirm concatenated-Markdown over a JSON array. If a machine-readable export is later wanted, add `?format=json` returning `list[KnowledgeArticleDetail]` — trivial follow-up, not in this plan.

---

## 2. Phase 1 — Backend: article content + export endpoints

### 2.1 Service methods (`backend/app/services/knowledge/knowledge_source_service.py`)

Add the `_get_source_for_read` helper (Decision B) near `get_source_by_id` (~L155).

**(1a) `get_article_content`** — single article full content:

```python
def get_article_content(
    *,
    session: Session,
    source_id: uuid.UUID,
    article_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[KnowledgeArticleDetail]:
    """Return one article's full content (incl. Markdown body) if the user may read the source.

    Returns None if the source is unreadable OR the article does not belong to it.
    """
    source = _get_source_for_read(session=session, source_id=source_id, user_id=user_id)
    if not source:
        return None
    article = session.get(KnowledgeArticle, article_id)
    if not article or article.git_repo_id != source_id:
        return None
    return KnowledgeArticleDetail(
        id=article.id,
        git_repo_id=article.git_repo_id,
        title=article.title,
        description=article.description,
        tags=article.tags,
        features=article.features,
        file_path=article.file_path,
        embedding_model=article.embedding_model,
        embedding_dimensions=article.embedding_dimensions,
        updated_at=article.updated_at,
        content=article.content,
        commit_hash=article.commit_hash,
    )
```

**(1b) `export_source_markdown`** — concatenated document:

```python
def export_source_markdown(
    *,
    session: Session,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[str]:
    """Return all of a source's articles concatenated into one Markdown document,
    or None if the source is unreadable. Empty source returns a valid (header-only) doc."""
    source = _get_source_for_read(session=session, source_id=source_id, user_id=user_id)
    if not source:
        return None
    articles = session.exec(
        select(KnowledgeArticle)
        .where(KnowledgeArticle.git_repo_id == source_id)
        .order_by(KnowledgeArticle.file_path)
    ).all()
    parts = [f"# {source.name}\n"]
    if source.description:
        parts.append(f"{source.description}\n")
    for a in articles:
        parts.append(f"\n---\n\n## {a.title}\n")
        parts.append(f"*Source file: `{a.file_path}`*\n")
        if a.description:
            parts.append(f"\n> {a.description}\n")
        parts.append(f"\n{a.content}\n")
    return "\n".join(parts)
```

Import `KnowledgeArticleDetail`, `KnowledgeArticle`, `SourceStatus` as needed (most already imported in the module).

### 2.2 Routes (`backend/app/api/routes/knowledge_sources.py`)

Add import `KnowledgeArticleDetail` to the existing model import block.

**Article content endpoint** (placed right after `list_knowledge_articles`, ~L249):

```python
@router.get(
    "/{source_id}/articles/{article_id}",
    response_model=KnowledgeArticleDetail,
)
def get_knowledge_article(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
    article_id: uuid.UUID,
) -> Any:
    """Get a single article's full content. Admin; owner OR public-discoverable read access."""
    article = knowledge_source_service.get_article_content(
        session=session,
        source_id=source_id,
        article_id=article_id,
        user_id=current_user.id,
    )
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return article
```

**Export endpoint:**

```python
from fastapi.responses import PlainTextResponse  # add to imports

@router.get("/{source_id}/export", response_class=PlainTextResponse)
def export_knowledge_source(
    *,
    session: SessionDep,
    current_user: SuperUser,
    source_id: uuid.UUID,
) -> Any:
    """Export all articles of a source as a single Markdown document.
    Admin; owner OR public-discoverable read access."""
    doc = knowledge_source_service.export_source_markdown(
        session=session,
        source_id=source_id,
        user_id=current_user.id,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return PlainTextResponse(
        content=doc,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="knowledge-source-{source_id}.md"'
        },
    )
```

> **Open decision (route ordering):** `/{source_id}/articles/{article_id}` must be registered so it does not shadow `/{source_id}/articles` (it won't — different path depth) and `/{source_id}/export` must not collide with `/{source_id}` PUT/GET (different suffix — fine). No ordering risk; noted for completeness.

> **Note on export response model:** `PlainTextResponse` produces an OpenAPI operation with no JSON schema body. The generated SDK method will return a string/blob. Frontend will fetch via raw `fetch` + JWT (see 3.3) rather than relying on the SDK for the download, to control the blob/Content-Disposition. The SDK method may still be generated; we will not depend on its body typing.

### 2.3 Authorization logic — spelled out
- Route layer: `SuperUser` (unchanged posture; only superusers ever reach these).
- Service layer (`_get_source_for_read`): grant when
  `source.user_id == user_id` **OR** (`source.public_discovery` **AND** `source.is_enabled` **AND** `source.status == SourceStatus.connected`).
- Article-not-in-source and source-unreadable both collapse to **404** (no existence leak distinction beyond what siblings already do).

---

## 3. Phase 2 — Frontend: preview modal + export button

### 3.1 Article preview modal (`KnowledgeSourceArticlesTab.tsx`)
- Make each `<TableRow>` clickable (`cursor-pointer`, `onClick={() => setSelected(article.id)}`).
- Add state `const [selectedArticleId, setSelectedArticleId] = useState<string | null>(null)`.
- On selection, render a `<Dialog>` (shadcn `@/components/ui/dialog`) with a `useQuery`:
  ```ts
  queryKey: ["knowledge-article", sourceId, selectedArticleId]
  queryFn: () => KnowledgeSourcesService.getKnowledgeArticle({ sourceId, articleId: selectedArticleId! })
  enabled: !!selectedArticleId
  ```
  (`getKnowledgeArticle` is the regenerated SDK method.)
- Dialog body renders title + a scroll container with `<MarkdownViewer content={data.content} />`
  (import from `@/components/Environment/MarkdownViewer`). Use `DialogContent` sized e.g.
  `max-w-3xl max-h-[80vh] overflow-hidden` with the MarkdownViewer's own `overflow-auto` handling scroll.
- Loading: skeleton inside the dialog.

### 3.2 Export button (`$sourceId.tsx` header dropdown)
- Add a `DropdownMenuItem` "Export as Markdown" (icon `Download` from lucide-react) above Edit, calling `handleExport`.
- `handleExport` performs an authenticated raw fetch (see 3.3), builds a `Blob`, triggers download via a temporary `<a>` element, then `showSuccessToast`. On non-2xx, `showErrorToast`.

### 3.3 Download helper (raw fetch + JWT)
Because the export is a file download (not JSON), use raw `fetch` with the bearer token from `localStorage["access_token"]` against `${VITE_API_URL}/api/v1/knowledge-sources/{sourceId}/export`, read `response.blob()`, and `URL.createObjectURL` → anchor click → revoke. This mirrors the established blob-download pattern used for file attachments (see memory: Agent Message Attachments — "blobs via raw fetch+JWT not SDK"). Filename from the `Content-Disposition` header, fallback `knowledge-source-${sourceId}.md`.

### 3.4 Client regeneration
After backend changes:
```bash
source ./backend/.venv/bin/activate && make gen-client
```
Confirms `getKnowledgeArticle` (and possibly `exportKnowledgeSource`) appear in `KnowledgeSourcesService`. Only `getKnowledgeArticle` is consumed via the SDK; export uses raw fetch.

---

## 4. Phase 3 — Problem 2: table horizontal overflow fix (CSS/layout only)

**Root cause:** `ui/table.tsx` `Table` wrapper is `overflow-x-auto`, and `TableCell`/`TableHead` default to `whitespace-nowrap`. A long description can't wrap → wrapper scrolls.

**Fix — scoped to `KnowledgeSourceArticlesTab.tsx` only** (do NOT change the shared `ui/table.tsx` primitive — it's used everywhere and `overflow-x-auto`/`whitespace-nowrap` are sensible global defaults):

1. Add `className="table-fixed w-full"` to `<Table>` (forces fixed layout so column widths are respected and content wraps instead of expanding the table).
2. Give the two columns explicit percentage widths on `<TableHead>`:
   - Title: `className="w-[30%]"`
   - Description: `className="w-[70%]"`
3. On the description `<TableCell>` (and the Title cell), override the nowrap default with wrapping:
   - Title cell: `className="font-medium whitespace-normal break-words align-top"`
   - Description cell: `className="whitespace-normal break-words align-top"`
   - Remove `line-clamp-2` from the description `<span>` (or keep it — see decision below).

> **Open decision for manager (P2-1):** keep `line-clamp-2` (truncate long descriptions to 2 lines in the table, full text visible in the preview modal) OR drop it (fully wrap in the table). Recommendation: **keep `line-clamp-2`** — cleaner table, full content one click away in the preview. With `table-fixed` + `whitespace-normal break-words`, line-clamp still works and no scrollbar appears.

Because the inner `<table>` now respects the wrapper width (`table-fixed w-full`) and cells wrap, the wrapper's `overflow-x-auto` never triggers a scrollbar. No change to the primitive needed; no new scrollbar introduced.

---

## 5. Tests (backend) — required

Extend `backend/tests/api/knowledge_sources/test_knowledge_sources.py` (API-only, superuser headers, follow existing scenario style; reuse `backend/tests/utils/knowledge_source.py` + the refresh/clone/embedding patches to populate articles). Add helpers to `tests/utils/knowledge_source.py` for `get_knowledge_article` and `export_knowledge_source`.

Cases:
1. **Article content — owner happy path:** create source, populate one article (via patched refresh), GET `/{id}/articles/{article_id}` → 200, body includes `content` + `commit_hash`.
2. **Article content — wrong article id / article from different source:** → 404.
3. **Article content — non-owner, source NOT discoverable:** second superuser → 404.
4. **Article content — non-owner, source `public_discovery=True` + enabled + connected:** second superuser → 200 (the broadened-access path; the crux of the feature).
5. **Export — owner happy path:** GET `/{id}/export` → 200, `content-type: text/markdown`, body contains source name + article title + article content; `Content-Disposition: attachment`.
6. **Export — empty source:** → 200, header-only doc (no crash).
7. **Export — non-owner discoverable:** → 200. **Non-owner non-discoverable:** → 404.
8. **Non-superuser rejection:** both endpoints reject a normal user (401/403), matching sibling-route behavior.

Run:
```bash
docker compose exec backend python -m pytest tests/api/knowledge_sources/test_knowledge_sources.py -v
```
Plus a regression sweep of `tests/api/knowledge_sources/`.

Frontend: manual check (no FE test harness for this area) — preview modal renders markdown, export downloads a `.md`, table no longer scrolls horizontally with a long description.

---

## 6. Implementation order (for the manager)

1. **Phase 1 backend** — `_get_source_for_read` + `get_article_content` + `export_source_markdown` in the service; two routes in `knowledge_sources.py`; import `KnowledgeArticleDetail` + `PlainTextResponse`.
2. **Phase 1 tests** — extend test file + utils; run knowledge_sources suite green.
3. **Client regen** — `make gen-client`; verify `getKnowledgeArticle` present.
4. **Phase 2 frontend** — preview modal in `KnowledgeSourceArticlesTab.tsx`; Export item + raw-fetch download in `$sourceId.tsx`.
5. **Phase 3 frontend** — table layout fix in `KnowledgeSourceArticlesTab.tsx` (`table-fixed`, column widths, `whitespace-normal break-words`).
6. **Verify** — typecheck touched files (`cd frontend && npx tsc --noEmit 2>&1 | grep -E "KnowledgeSourceArticlesTab|sourceId" | head`), manual UI check.

---

## 7. Files touched (exhaustive)

**Backend:**
- `backend/app/services/knowledge/knowledge_source_service.py` — add `_get_source_for_read`, `get_article_content`, `export_source_markdown`.
- `backend/app/api/routes/knowledge_sources.py` — add 2 routes + imports.
- `backend/tests/api/knowledge_sources/test_knowledge_sources.py` — add scenarios.
- `backend/tests/utils/knowledge_source.py` — add `get_knowledge_article`, `export_knowledge_source` helpers.

**Models:** NONE changed (reuse `KnowledgeArticleDetail`). NO migration.

**Frontend:**
- `frontend/src/components/KnowledgeSources/KnowledgeSourceArticlesTab.tsx` — preview modal + table layout fix.
- `frontend/src/routes/_layout/knowledge-source/$sourceId.tsx` — Export dropdown item + download helper.
- `frontend/src/client/*` — regenerated (auto).
- `ui/table.tsx` — **NOT** modified (intentional).

---

## 8. Open decisions summary (for manager)

- **A1** — Interpret "shared with them" as **`public_discovery`** (no sharing table); keep routes `SuperUser`. Genuine non-superuser sharing is out of scope. **Confirm.**
- **C1** — Export format = **concatenated Markdown**. Confirm vs JSON array.
- **P2-1** — Keep `line-clamp-2` on the table description (full text in preview modal) vs full wrap. Recommendation: keep.
