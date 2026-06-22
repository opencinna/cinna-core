# User Locale Preferences + Agent Conversation Style — Implementation Plan

## Overview

Add four user-profile preferences that personalize how every one of a user's agents communicates:

- **`timezone`** — IANA zone (e.g. `Europe/Berlin`) for date/time formatting.
- **`language`** — preferred *communication* language (e.g. `en` / `English`).
- **`locale`** — *formatting* locale (e.g. `en-US`, `de-DE`) for date/time/number formatting. Deliberately **distinct** from `language`.
- **`conversation_style`** — one of `ai_default` / `concise_direct` / `friendly_chatty`. Adjusts agent tone.

These four fields surface in two places:

- **A. credentials.json `current_user` block** (all four), so scripts and the agent prompt can read them.
- **B. the conversation-mode system prompt** (only `conversation_style`, only for the two non-default values), so the agent adopts a tone.

Changing any of the four re-syncs every running environment of every agent the user owns — exactly like the existing **User's Details** fan-out. Browser login auto-detects and persists `timezone`/`language`/`locale` **only when still NULL** (never clobbering an explicit choice); `conversation_style` is never auto-detected.

```
User profile (4 new fields)
   │
   ├── PATCH /users/me ──────────────► user row updated ──► (if any of the 4 / details changed) ──► event_user_details_updated fan-out
   │                                                                                                       │
   ├── PATCH /users/me/locale-defaults (browser auto-detect, NULL-only fill) ──► only-changed fan-out      │
   │                                                                                                       ▼
   └──────────────────────────────────────────────────────► CredentialsService.sync_credentials_to_agent_environments(agent)
                                                                          │
                                                                          ▼
                                          build_current_user_block(user) → credentials.json `current_user.credential_data`
                                          generate_credentials_readme()  → credentials/README.md "## Current User"
                                                                          │
                                                          (inside container, at next prompt build)
                                                                          ▼
                                          prompt_generator.generate_conversation_mode_prompt()
                                              reads conversation_style from credentials.json `current_user`
                                              appends ONE tone sentence (conversation mode only; ai_default = nothing)
```

---

## Architecture Overview

This feature is a thin extension of two existing, fully-built features:

- **`agent_credentials` → `current_user` record** (`docs/agents/agent_credentials/agent_credentials.md` / `_tech.md`). The synthetic `current_user` entry already carries `username`/`full_name`/`email`/`email_confirmed`/`custom_details`. We add four keys to `credential_data` and one new prose paragraph to the README generator.
- **`agent_prompts` → conversation-mode prompt** (`docs/agents/agent_prompts/agent_prompts_tech.md`). `prompt_generator.generate_conversation_mode_prompt()` runs **inside the container** and already loads `credentials/README.md`. We add a single tone sentence read from the `current_user` block of `credentials.json`.

No new tables, no new services, no new event types. The only persistence change is four columns on `user`. The fan-out re-sync mechanism (`event_user_details_updated`) already exists and is reused verbatim.

**Integration points touched:**

| System | File | Change |
|--------|------|--------|
| User model/schemas | `backend/app/models/users/user.py` | 4 fields on `UserBase`-adjacent models + `ConversationStyle` enum + `UserLocaleDefaults` schema |
| Migration | `backend/app/alembic/versions/` | add 4 columns; `conversation_style` server_default `ai_default` |
| current_user block | `backend/app/services/users/user_details_service.py` | extend `build_current_user_block` |
| credentials README | `backend/app/services/credentials/credentials_service.py` | extend `generate_credentials_readme` "## Current User" section |
| route (update) | `backend/app/api/routes/users.py` | `update_user_me` validates `conversation_style`, triggers fan-out on change |
| route (new) | `backend/app/api/routes/users.py` | `PATCH /users/me/locale-defaults` NULL-only fill + conditional fan-out |
| conversation prompt | `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` | append tone sentence (env-template → **rebuild required**) |
| frontend UI | `frontend/src/components/UserSettings/UserInformation.tsx` (+ optional sibling card) | 4 selectors wired into `UserUpdateMe` |
| frontend auth | `frontend/src/hooks/useAuth.ts`, `frontend/src/components/Auth/GoogleLoginButton.tsx` | post-login `locale-defaults` call |
| generated client | `frontend/src/client/` | regenerate after backend changes |

---

## Data Models

### `user` table — four new columns

File: `backend/app/models/users/user.py`.

| Column | Type | Nullable | Default / server_default | Notes |
|--------|------|----------|--------------------------|-------|
| `timezone` | `VARCHAR(64)` | yes | `NULL` | IANA zone string, e.g. `Europe/Berlin`. No DB-level enum (IANA set is large + evolving). |
| `language` | `VARCHAR(64)` | yes | `NULL` | Communication language, e.g. `en` or `English`. Free-text. |
| `locale` | `VARCHAR(64)` | yes | `NULL` | BCP-47 formatting locale, e.g. `en-US`, `de-DE`. Free-text. |
| `conversation_style` | `VARCHAR(32)` | **no** | server_default `'ai_default'` | Validated against `ConversationStyle` enum at the route layer. |

`timezone`/`language`/`locale` are intentionally free-text strings (not FK / not DB enum): the curated lists live in the frontend; the backend accepts the detected value verbatim. Keep lengths generous (64) — BCP-47 + IANA strings are short but can be compound.

`conversation_style` is **NOT NULL** with a server_default so existing rows backfill to `ai_default` (current behavior) without a data-migration pass.

### `ConversationStyle` enum

Follow the existing `UserRole(str, Enum)` pattern already in this file (lines 26-33):

```python
class ConversationStyle(str, Enum):
    AI_DEFAULT = "ai_default"
    CONCISE_DIRECT = "concise_direct"
    FRIENDLY_CHATTY = "friendly_chatty"

VALID_CONVERSATION_STYLES = [s.value for s in ConversationStyle]
```

### Model placement (mirror the existing `role` / details fields)

- **`User(table=True)`** (lines 87-152): add the four physical columns.
  - `timezone: str | None = Field(default=None, max_length=64)`
  - `language: str | None = Field(default=None, max_length=64)`
  - `locale: str | None = Field(default=None, max_length=64)`
  - `conversation_style: str = Field(default=ConversationStyle.AI_DEFAULT.value, max_length=32)`
- **`UserUpdateMe`** (lines 65-78): add all four as optional so the profile editor can set them.
  - `timezone: str | None = Field(default=None, max_length=64)`
  - `language: str | None = Field(default=None, max_length=64)`
  - `locale: str | None = Field(default=None, max_length=64)`
  - `conversation_style: str | None = Field(default=None, max_length=32)`
- **`UserPublic`** (lines 156-181): add all four so the UI can render current values and the frontend can decide which are NULL.
  - `timezone: str | None = None`
  - `language: str | None = None`
  - `locale: str | None = None`
  - `conversation_style: str = ConversationStyle.AI_DEFAULT.value`
- **New schema `UserLocaleDefaults`** (request body for the auto-detect endpoint):

```python
class UserLocaleDefaults(SQLModel):
    """Browser-detected locale defaults; server fills only still-NULL fields."""
    timezone: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, max_length=64)
```

> Note: `conversation_style` is deliberately **absent** from `UserLocaleDefaults` — it is never browser-detected.

---

## Security Architecture

- **No secrets.** All four fields are non-sensitive personalization. They are written to `credentials.json` `current_user` and to the **unredacted** README `## Current User` section (same as the existing identity fields — `current_user` bypasses `AGENT_ENV_ALLOWED_FIELDS` and the `SENSITIVE_FIELDS` redaction; see `agent_credentials_tech.md` lines 99-114).
- **Ownership.** Every write is `current_user`-scoped via `CurrentUser`. Both routes mutate only `current_user`'s own row. No admin path, no cross-user surface.
- **NULL-only fill guard (load-bearing).** `PATCH /users/me/locale-defaults` MUST write a field **only when the stored value is currently `None`**. This prevents a later browser session (different machine/locale) from silently overwriting a deliberate setting the user picked in Settings. The guard is server-side and authoritative — never trust the client to decide.
- **Validation.** `conversation_style`, when present on `PATCH /users/me`, is validated against `VALID_CONVERSATION_STYLES` → `HTTP 400` on mismatch (mirror the existing SDK validation in `update_user_me`, lines 213-227). `timezone`/`language`/`locale` are length-capped (`max_length=64`) free-text; no further validation (the curated picker constrains values in practice, and a bad IANA string degrades gracefully — the agent simply can't format with it).
- **Fan-out is failure-isolated.** `event_user_details_updated` already swallows per-env errors and filters to running envs; reuse unchanged.

---

## Backend Implementation

### 1. Model + enum changes

`backend/app/models/users/user.py` — as specified in **Data Models** above. Add `ConversationStyle` enum near `UserRole` (line ~26). Re-export is automatic (already exported via `app.models`). No `models/__init__.py` change needed (these are additions to an existing exported module).

### 2. `build_current_user_block` extension

File: `backend/app/services/users/user_details_service.py`, function `build_current_user_block(user)` (lines 162-182).

Add the four keys to `credential_data` (null when unset is fine):

```python
"credential_data": {
    "username": user.username,
    "full_name": user.full_name,
    "email": user.email,
    "email_confirmed": user.email_confirmed,
    "timezone": user.timezone,
    "language": user.language,
    "locale": user.locale,
    "conversation_style": user.conversation_style,
    "custom_details": user.details_parsed or {},
},
```

`conversation_style` is always present (non-null column). The other three are `None` when unset — that is acceptable; the README copy tells the agent that absence means "no preference".

### 3. `generate_credentials_readme` "## Current User" copy

File: `backend/app/services/credentials/credentials_service.py`, inside the `type == "current_user"` block of `generate_credentials_readme` (the `## Current User` section, ~lines 812-844).

Add, after the existing identity prose and before/within the access snippet, copy that tells the agent these fields exist and how to honor them. Suggested additions (the exact wording can be refined; the **intent** is load-bearing):

> **Communication preferences.** The `current_user` entry also carries the owner's communication preferences:
> - `language` — the language to communicate in. If set, write your responses to the user in this language. If `null`, use your default.
> - `locale` — a BCP-47 formatting locale (e.g. `en-US`, `de-DE`). When you present dates, times, and numbers, format them according to this locale. If `null`, use a sensible default.
> - `timezone` — an IANA timezone (e.g. `Europe/Berlin`). Express dates and times in this timezone. If `null`, do not assume one.
> - `conversation_style` — a tone hint (`ai_default`, `concise_direct`, or `friendly_chatty`). `ai_default` means no adjustment.

Extend the existing Python access snippet so the agent sees how to read them:

```python
creds = {c['id']: c['credential_data'] for c in all_credentials}
me = creds['current_user']
send_to = me['email']
language = me.get('language')          # communicate in this language
locale = me.get('locale')             # format dates/times/numbers with this
tz = me.get('timezone')               # express times in this IANA zone
detail = me['custom_details'].get('SOME_KEY')
```

> This README change reaches the **building-mode** prompt and conversation-mode prompt context for free (both load `credentials/README.md`). It does NOT itself add the tone sentence — that is item B (prompt_generator), below.

### 4. `update_user_me` route — validation + fan-out

File: `backend/app/api/routes/users.py`, `update_user_me` (lines 192-264).

**(a) Validate `conversation_style`** (add alongside the existing SDK validations ~line 213):

```python
if user_in.conversation_style is not None and user_in.conversation_style not in VALID_CONVERSATION_STYLES:
    raise HTTPException(status_code=400, detail=f"Invalid conversation style. Must be one of: {VALID_CONVERSATION_STYLES}")
```

**(b) Trigger the re-sync fan-out when any of the four (or details) change.** Currently `update_user_me` does **NOT** re-sync running environments (confirmed: lines 259-264 just `sqlmodel_update` + commit + return). The plan adds it.

Approach (compute *before* applying the update so we can diff):

```python
# Before sqlmodel_update:
personalization_fields = ("timezone", "language", "locale", "conversation_style")
incoming = user_in.model_dump(exclude_unset=True)
personalization_changed = any(
    field in incoming and getattr(current_user, field) != incoming[field]
    for field in personalization_fields
)
# ... existing sqlmodel_update / add / commit / refresh ...
if personalization_changed:
    await user_details_service.event_user_details_updated(session=session, user_id=current_user.id)
```

> **`async` note:** `event_user_details_updated` is `async`. `update_user_me` is currently a sync `def`. Two options:
> - **Preferred:** make `update_user_me` `async def` (matches `update_user_details` at line 324 which is already `async def` and calls the same fan-out). Verify no caller depends on it being sync (it's a FastAPI route — safe).
> - Alternative: schedule the fan-out the same way `/me/details` does (read that handler at lines 324-374 and mirror its exact invocation, incl. any `BackgroundTasks` usage). **Match `/me/details` precisely** so concurrency semantics are identical.

### 5. New route `PATCH /users/me/locale-defaults`

File: `backend/app/api/routes/users.py` (add near `/me/details`, after line 374).

```python
@router.patch("/me/locale-defaults", response_model=UserPublic)
async def update_user_locale_defaults(
    *, session: SessionDep, defaults_in: UserLocaleDefaults, current_user: CurrentUser
) -> Any:
    """Fill browser-detected timezone/language/locale ONLY where still unset.

    Idempotent and clobber-safe: a field is written only when the stored
    value is currently NULL, so an explicit user choice is never overwritten.
    """
    changed = False
    for field in ("timezone", "language", "locale"):
        incoming = getattr(defaults_in, field)
        if incoming and getattr(current_user, field) is None:
            setattr(current_user, field, incoming)
            changed = True
    if changed:
        session.add(current_user)
        session.commit()
        session.refresh(current_user)
        await user_details_service.event_user_details_updated(session=session, user_id=current_user.id)
    return _user_to_public(session, current_user)
```

- **Request body:** `UserLocaleDefaults { timezone?, language?, locale? }`.
- **Response:** `UserPublic` (so the frontend can read back the now-populated values and update its cache).
- **Deps:** `SessionDep`, `CurrentUser`. No admin guard.
- **Status codes:** `200` always (idempotent no-op when nothing was NULL). `422` on malformed body (length > 64).
- **Fan-out:** only when `changed` (so a no-op login call does not thrash every env).

> Decision: keep this as a **dedicated minimal endpoint** rather than overloading `PATCH /users/me`. The NULL-only semantics are specific to auto-detection and must never apply to the explicit profile editor (which must be able to clear/overwrite). Separate endpoints keep the two semantics from leaking into each other.

### Service Layer

No new service. Reuses:
- `user_details_service.build_current_user_block` (extended).
- `user_details_service.event_user_details_updated` (unchanged) → `CredentialsService.sync_credentials_to_agent_environments`.
- `CredentialsService.generate_credentials_readme` (extended).

### Background Tasks

None new. The fan-out re-uses the existing per-agent sync, which already filters to running environments and swallows per-env errors (idempotent — re-running just re-writes the same `credentials.json`).

---

## Env-Template / In-Container Change (item B)

> ⚠️ **Environment rebuild required.** `prompt_generator.py` lives under `backend/app/env-templates/app_core_base/core/server/` and is baked into the agent Docker image. Changes take effect **only after the environment image is rebuilt and the env recreated**. Call this out in the PR and docs.

File: `backend/app/env-templates/app_core_base/core/server/prompt_generator.py`, method `generate_conversation_mode_prompt` (lines 633-727).

**How the generator reads `conversation_style`:** the generator already loads `credentials/README.md` via `_load_credentials_readme()` (lines 171-198), but the README is prose. The structured value lives in `credentials/credentials.json`. The cleanest read is a small helper that loads the JSON and finds the `current_user` entry (mirror `_load_handover_prompt`'s `json.load` pattern at lines 200-215):

```python
def _load_conversation_style(self) -> Optional[str]:
    """Read current_user.conversation_style from credentials.json.

    Returns the style string, or None if absent/unreadable. Never raises.
    """
    creds_path = self.workspace_dir / "credentials" / "credentials.json"
    if not creds_path.exists():
        return None
    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        for entry in entries:
            if entry.get("type") == "current_user":
                return (entry.get("credential_data") or {}).get("conversation_style")
    except Exception as e:
        logger.warning(f"Could not read conversation_style: {e}")
    return None
```

Then, near the end of `generate_conversation_mode_prompt` (before combining `conversation_prompt_parts`), append exactly **one** sentence for the two non-default styles, and **nothing** for `ai_default`:

```python
style = self._load_conversation_style()
style_sentence = {
    "concise_direct": (
        "Communicate concisely and directly: keep responses brief and to the "
        "point, avoiding unnecessary elaboration."
    ),
    "friendly_chatty": (
        "Adopt a warm, friendly, and conversational tone in your responses."
    ),
}.get(style or "")
if style_sentence:
    conversation_prompt_parts.append(f"\n\n---\n\n## Communication Style\n\n{style_sentence}")
    logger.info(f"Appended conversation style sentence for '{style}'")
```

**Constraints (must hold):**
- `ai_default` → append nothing (current behavior, zero prompt change).
- Conversation mode **only** — do **not** touch `generate_building_mode_prompt` (lines ~588-632). Building mode is unaffected.
- Exactly one sentence; no multi-paragraph tone essays.

> The README copy (item A.3) already explains all four fields including `conversation_style`, so the building-mode prompt is aware they exist; only the conversation-mode prompt gets the actual behavioral tone sentence.

---

## Frontend Implementation

### UI — Settings → My profile

File: `frontend/src/components/UserSettings/UserInformation.tsx` (currently edits `username` / `full_name` via a dialog) — extend its edit form, **or** add a small sibling card (e.g. `UserPreferences.tsx`) rendered next to it in `settings.tsx`. Either is acceptable; a sibling card keeps the profile dialog uncluttered. Decision left to implementer — the sibling-card approach is recommended because the four selectors plus the IANA timezone list are visually heavy for the existing two-field dialog.

**Selectors:**

1. **Timezone** — `Select`/combobox populated from `Intl.supportedValuesOf("timeZone")` (guard with a fallback list if unsupported). Include the browser-detected value (`Intl.DateTimeFormat().resolvedOptions().timeZone`) as the suggested option. Allow "Not set" (clears to `null`).
2. **Language** — `Select` from a curated list (e.g. en/de/fr/es/it/pt/nl/…), labeled with the human name. Allow the detected `navigator.language` primary subtag even if not in the curated list. Allow "Not set".
3. **Locale** — `Select` from a curated BCP-47 list (`en-US`, `en-GB`, `de-DE`, `fr-FR`, …). Allow the detected `navigator.language` (full tag). Allow "Not set". UI copy should make clear this is for **date/time/number formatting**, distinct from Language.
4. **Conversation style** — `Select` / radio group, 3 options:
   - `ai_default` → "AI Default (no adjustments)"
   - `concise_direct` → "Concise and direct"
   - `friendly_chatty` → "Friendly and chatty"

**Wiring:** build a `UserUpdateMe` partial in `onSubmit` (mirror the existing diff pattern at lines 134-145) and submit via the existing `UsersService.updateUserMe({ requestBody })` mutation (lines 69-80). The mutation already `invalidateQueries()` on settle, which refreshes `currentUser`.

- Use `react-hook-form` + `zod` per project convention. Zod schema: `conversation_style: z.enum(["ai_default","concise_direct","friendly_chatty"]).optional()`; the other three `z.string().max(64).optional().or(z.literal(""))` with `""` → `null` on submit (so "Not set" clears the value).
- Default values pulled from `currentUser?.timezone` etc.

### State Management

- React Query, query key `["currentUser"]` (already used by `useAuth`). The existing `updateUserMe` mutation + `invalidateQueries()` cover the profile-edit path. The auto-detect path (below) also invalidates `["currentUser"]` on success so the UI reflects populated values.

### Browser auto-detection on login

Goal: on **browser** login, persist `timezone`/`language`/`locale` **iff currently null**. Implemented by calling the new `PATCH /users/me/locale-defaults` endpoint right after a token is stored. The server enforces the NULL-only guarantee, so the client can safely send detected values every login.

Detected values:
- `timezone`: `Intl.DateTimeFormat().resolvedOptions().timeZone`
- `language`: `navigator.language` (primary subtag, e.g. `navigator.language.split("-")[0]`)
- `locale`: `navigator.language` (full BCP-47 tag)

**Two call sites** (both store the token then redirect):

1. **Password login** — `frontend/src/hooks/useAuth.ts`, `loginMutation.onSuccess` (lines 191-205): after `localStorage.setItem("access_token", …)` (line 200) and before/after `navigateToPostAuthTarget`, fire a fire-and-forget `UsersService.updateUserMeLocaleDefaults({ requestBody: detected })`. Do **not** block navigation on it; swallow errors (best-effort).
2. **Google OAuth** — `frontend/src/components/Auth/GoogleLoginButton.tsx`, the `onSuccess` that stores the token (line 55, `localStorage.setItem("access_token", data.access_token)`): same fire-and-forget call before `navigate({ to: "/" })` (line 61).

Recommended: extract a tiny shared helper `persistDetectedLocaleDefaults()` (e.g. in `useAuth.ts` or a `utils` module) that builds the detected payload and calls the regenerated `UsersService.updateUserMeLocaleDefaults`, used by both sites to avoid drift. Skip the call when the SDK function name resolves post-regen.

> `conversation_style` is **not** auto-detected — it defaults to `ai_default` server-side and is only ever set via the profile selector.

### User Flows

- **New browser user logs in** → token stored → `locale-defaults` called with detected tz/lang/locale → server fills the (NULL) fields → next env sync carries them. User sees pre-filled values in Settings.
- **User overrides in Settings** → `PATCH /users/me` → fields updated (can clear to null) → fan-out re-syncs envs → agent picks up new tone/locale on next conversation-mode prompt build (already-built envs get the new `credentials.json`; the tone sentence requires the rebuilt image for the prompt-generator change — see env-rebuild note).
- **Same user logs in from a second machine with a different locale** → `locale-defaults` is a no-op (fields no longer NULL) → explicit choice preserved.

---

## Database Migrations

- **Create via Docker** (per CLAUDE.md): `make migration` (or `docker compose exec backend alembic revision --autogenerate -m "add user locale prefs and conversation style"`).
- **Review autogen for drift** — autogenerate will emit `add_column` for all four. Hand-verify:
  - `timezone` / `language` / `locale`: `sa.Column(..., sa.String(length=64), nullable=True)`.
  - `conversation_style`: `nullable=False, server_default="ai_default"`. Confirm the `server_default` is present (it backfills existing rows). Optionally drop the server_default in a follow-up line after backfill, but leaving it is harmless and keeps inserts that omit the column valid.
- **Indexes:** none required (these are never queried/filtered — only read with the user row).
- **Foreign keys:** none.
- **Single head:** repo has had multi-head situations historically. After generating, run `docker compose exec backend alembic heads` and confirm a single head; add a merge migration only if a second head exists (do not let this migration silently create one). Set `down_revision` to the current single head.
- **Downgrade:** `op.drop_column` for all four columns.

---

## Error Handling & Edge Cases

- **Invalid `conversation_style`** on `PATCH /users/me` → `400` with the allowed list.
- **Over-length locale/tz/lang** → `422` (pydantic `max_length=64`).
- **`locale-defaults` no-op** (all already set) → `200`, no fan-out, no DB write.
- **Detected timezone unsupported in old browsers** → `Intl.supportedValuesOf` may be undefined; the picker must fall back to a static list and the detection helper must guard `typeof Intl.supportedValuesOf === "function"`.
- **Agent can't honor a locale/timezone** → graceful: a malformed IANA string just means the agent can't format with it; nothing breaks. The README copy says "use a sensible default" when absent.
- **Fan-out during high agent count** → existing `event_user_details_updated` enumerates owned agents and syncs running envs only, swallowing per-env errors. No change.
- **`ai_default` must remain a true no-op** — verify the prompt-generator branch appends nothing and emits an identical prompt to the pre-feature build.

---

## UI/UX Considerations

- Group the four under a "Communication & Locale" card/section with short helper text per field. Critically, distinguish **Language** ("what language the agent talks to you in") from **Locale** ("how dates/times/numbers are formatted") with inline descriptions — these are the two most likely to be confused.
- Conversation style: render the three labels exactly as specified ("AI Default (no adjustments)", "Concise and direct", "Friendly and chatty"). Default-selected = AI Default.
- "Not set" / clear option for tz/language/locale (maps to `null`).
- After save, the existing success toast + query invalidation suffice; no special confirmation needed.

---

## Integration Points

- **`agent_credentials`** — the `current_user` block + README. The four fields ride the existing sync/whitelist-bypass/no-redaction path. No new credential plumbing.
- **`agent_prompts`** — conversation-mode prompt assembly. One new optional section appended after the existing parts.
- **Client regeneration (required):** after backend model/route changes, regenerate the OpenAPI client so `UserUpdateMe`/`UserPublic` carry the new fields and `UsersService.updateUserMeLocaleDefaults` exists:
  ```bash
  source ./backend/.venv/bin/activate && make gen-client
  ```
- **Env rebuild (required for tone):** the `prompt_generator.py` change only takes effect after rebuilding the env image and recreating environments. The credentials.json/README changes (items A) propagate via normal credential sync without a rebuild; only the **tone sentence** (item B) needs the rebuild.

---

## Documentation Updates

- `docs/agents/agent_credentials/agent_credentials.md` — note that the `current_user` block now also carries `timezone`/`language`/`locale`/`conversation_style`.
- `docs/agents/agent_credentials/agent_credentials_tech.md` — update the `build_current_user_block` description (line ~118) to list the four new `credential_data` keys, and the `generate_credentials_readme` `## Current User` description (line ~101) to mention the communication-preferences prose.
- `docs/agents/agent_prompts/agent_prompts_tech.md` — update the `generate_conversation_mode_prompt` assembly order (line ~118) to include the optional "Communication Style" sentence read from `credentials.json` `current_user`.
- `docs/README.md` glossary — extend the **`current_user` record** entry (line 52) to mention the four new fields, and consider a one-line note on the **User's Details** entry that the same fan-out covers locale/style changes.

---

## Future Enhancements (Out of Scope)

- Locale-aware formatting enforced by the platform (today it's agent-honored via prompt, not platform-rendered).
- Per-agent conversation-style override (currently user-global).
- Building-mode tone adjustment (intentionally conversation-mode only).
- Richer style taxonomy (formal/technical/etc.) — the enum is extensible by adding values + sentences.
- Detecting `conversation_style` heuristically (intentionally never auto-detected).

---

## Phased Plan & Summary Checklist

### Phase 1 — Models + Migration
- [ ] Add `ConversationStyle(str, Enum)` + `VALID_CONVERSATION_STYLES` to `user.py`.
- [ ] Add `timezone`/`language`/`locale`/`conversation_style` to `User(table=True)`, `UserUpdateMe`, `UserPublic`.
- [ ] Add `UserLocaleDefaults` schema (3 fields, no `conversation_style`).
- [ ] Generate migration via Docker; review autogen drift; confirm `conversation_style` `nullable=False server_default='ai_default'`; confirm single alembic head.

### Phase 2 — Backend service + routes
- [ ] Extend `build_current_user_block` with the four keys.
- [ ] Extend `generate_credentials_readme` `## Current User` section + access snippet.
- [ ] `update_user_me`: validate `conversation_style`; diff the four fields; trigger `event_user_details_updated` on change; resolve sync-vs-async (prefer `async def`, mirror `/me/details`).
- [ ] Add `PATCH /users/me/locale-defaults` (NULL-only fill + conditional fan-out, `UserPublic` response).

### Phase 3 — Env-template (prompt_generator)
- [ ] Add `_load_conversation_style()` reading `credentials.json` `current_user`.
- [ ] Append the single tone sentence in `generate_conversation_mode_prompt` for `concise_direct` / `friendly_chatty` only; nothing for `ai_default`; building mode untouched.
- [ ] **Flag env rebuild requirement** in the PR.

### Phase 4 — Frontend UI + auth
- [ ] Add the four selectors (timezone via `Intl.supportedValuesOf`, curated language/locale, 3-option conversation style) in `UserInformation.tsx` or a sibling `UserPreferences` card; wire to `UsersService.updateUserMe`.
- [ ] Add `persistDetectedLocaleDefaults()` helper; call from password-login `onSuccess` (`useAuth.ts`) and Google OAuth `onSuccess` (`GoogleLoginButton.tsx`), fire-and-forget.
- [ ] **Regenerate client** (`make gen-client`) before using new SDK methods/types.

### Phase 5 — Docs + tests
- [ ] Update the four docs listed above.
- [ ] Backend API tests (below).

### Testing & Validation Tasks (API-only, per `backend/tests/README.md`)
- [ ] `PATCH /users/me` with each `conversation_style` value persists and is returned by `GET /users/me`.
- [ ] `PATCH /users/me` with an invalid `conversation_style` → `400`.
- [ ] `PATCH /users/me` setting `timezone`/`language`/`locale` persists; clearing to `null` works.
- [ ] `PATCH /users/me/locale-defaults` fills fields when NULL; **does not** overwrite when already set (the load-bearing guard).
- [ ] `PATCH /users/me/locale-defaults` no-op (all set) returns `200` and changes nothing.
- [ ] Over-length value → `422`.
- [ ] (If feasible via the env/test harness) assert the `current_user` block / README the credentials pipeline produces includes the four keys. Otherwise cover `build_current_user_block` shape via the credentials-readme/env-sync test path used by existing `current_user` tests.
- [ ] Regression: existing `current_user` / User's Details tests still pass.

---

## Open Questions / Risks

1. **`update_user_me` sync→async.** Making it `async def` is the clean fix (matches `/me/details`). Confirm no internal caller invokes it as a plain function. **Read lines 324-374 (`/me/details`) and mirror its exact fan-out invocation** (it may use `BackgroundTasks` or a direct `await` — match it precisely for identical concurrency behavior).
2. **prompt_generator reading credentials.json.** Confirmed the generator runs in-container and already reads workspace files (`_load_handover_prompt` uses `json.load`). Reading `credentials/credentials.json` directly is consistent — but verify the file is always present at conversation-prompt build time (it is written by the credential sync at env start). If absent, `_load_conversation_style` returns `None` → no sentence (safe).
3. **`UserUpdateMe` validation of the enum.** Chosen approach validates in the route (matches existing SDK-validation style) rather than a pydantic validator, to keep the 400 message consistent with siblings. If a stricter schema-level constraint is preferred, a pydantic `field_validator` on `UserUpdateMe.conversation_style` is an alternative — but then the failure is `422` not `400`; pick one and be consistent.
4. **Fan-out cost on profile save.** Every personalization change re-syncs all owned agents' running envs. This mirrors User's Details exactly and is acceptable, but a user editing several fields in one save triggers a single fan-out (good — we diff once and fire once).
5. **Locale vs Language confusion** is the main UX risk — mitigate with explicit inline copy (see UI/UX).
6. **Env-rebuild lag for the tone sentence.** Until envs are rebuilt, existing agents will carry the four fields in credentials.json/README (so scripts and building-mode awareness work) but will NOT yet apply the conversation-mode tone sentence. Communicate this clearly so it isn't mistaken for a bug.
