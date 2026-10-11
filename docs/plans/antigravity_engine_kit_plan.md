# Plan: the Local Agent Kit knows the `antigravity` engine

Source: `drafts/antigravity_engine_support.md` (Cinna Desktop report, desktop commits
`6dc54eae`..`27727173`). Desktop already treats `antigravity` as a known, CLI-login
engine (`../cinna-desktop/src/shared/engine.ts:140-147`, `src/main/kit/validator.ts:354`).
Core's `kit.py` still warns "not a known engine … runs on OpenCode instead". No UI, no
migration, no API change, no client regeneration.

## §0 Index

### Working-tree rules

- Shared tree on `main`; touch only the files listed below. Never hand-edit the
  mirror under `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/`
  or `backend/app/env-templates/app_core_base/core/prompts/AGENT_DESIGN_PATTERNS.md`;
  they are produced by the sync in Phase 3.
- Do **not** run the full `make sync-platform-knowledge` (see D3).

### Decisions taken (recommendation marked R)

| # | Decision | Taken |
|---|----------|-------|
| D1 | Contract version | **R: no contract bump; kit-only CHANGELOG entry.** `CONTRACT_VERSION` stays `1.10.0`. Reasons in §4. Alternative (minor bump to 1.11.0) listed in §6 for the user. |
| D2 | `assistants/antigravity.md` | **R: defer.** It is for people who *author* folders with Antigravity; running agents on it needs nothing. Revisit when someone does. |
| D3 | Mirror sync scope | **R: run only the kit and design-patterns steps of the sync script, plus copy the one edited business doc.** The full sync also rebuilds `knowledge/platform/` from `docs/application`, `docs/agents`, `docs/flows` and `frontend/openapi.json`, and that tree has unrelated drift today (`new_agent_wizard.md`, `agent_bundles.md`, `credential_sharing.md`, `agent_skills.md` differ; `google_chat_integration/`, `team_library/` missing). That drift belongs in its own commit. |
| D4 | Conformance case shape | Copy the codex pair exactly: the "known" case uses `expect.no_warnings: ["runtime.engine"]`, not `warnings: []` (report is wrong, §5). |

### Phase table

| Phase | Files touched | Plan section | Tests / checks |
|-------|---------------|--------------|----------------|
| 1 Kit source | `docs/local_agent_kit/tools/kit.py`, `schema/cinna-agent.schema.json`, `conformance/manifests/` (+2 files), `guides/13-design-patterns.md`, `CHANGELOG.md` | §1 | conformance + kit tool unit tests (after Phase 3) |
| 2 Backend comments, tests, feature docs | `backend/app/services/environments/sdk_constants.py`, `backend/app/models/cli/account_convenience.py`, `backend/tests/unit/test_sdk_engine_preference.py`, `backend/tests/unit/test_local_kit_tool.py`, `docs/application/local_agent_kit/local_agent_kit_tech.md`, `docs/application/cinna_cli_integration/account_cli_workspace.md` | §2 | `test_sdk_engine_preference.py` |
| 3 Mirror sync + verify | generated: `.../knowledge/local-kit/**`, `.../app_core_base/core/prompts/AGENT_DESIGN_PATTERNS.md`, `.../knowledge/platform/application/cinna_cli_integration/account_cli_workspace.md` | §3 | full checklist §3 |

One developer can do all three phases in order; total diff is ~60 lines.

## §1 Phase 1 — kit source

1. `docs/local_agent_kit/tools/kit.py:226-228`
   - `KNOWN_ENGINES = ("opencode", "claude", "codex", "antigravity")`
   - `CLI_LOGIN_ENGINES = ("claude", "codex", "antigravity")`
   - Comment at `:1618-1623` ("`claude`/`codex` ignore the credential"): name the
     CLI-login engines generically, e.g. "an engine in `CLI_LOGIN_ENGINES` ignores the
     credential", so the next engine does not need a comment edit.
   - Messages at `:1630-1641` unchanged. The unknown-engine message builds its list
     from `KNOWN_ENGINES`, so it now prints four names; the CLI-login message already
     reads "`runtime.engine` antigravity uses its own CLI login, so runtime.credential
     is ignored. Remove it." (desktop parity is by field path only, per
     `conformance/README.md` "Matching rule"; wording already matches the desktop's).
2. `docs/local_agent_kit/schema/cinna-agent.schema.json:115-117` (`runtime.engine`)
   - "Known values" sentence: add "`antigravity` (Google Antigravity's own ACP server,
     under the user's Google login)"; "`claude`, `codex` and `antigravity` drive a CLI
     or agent server under its own login".
   - Credential sentence: "`claude`/`codex`/`antigravity` and `credential` are not
     meaningful together".
   - `examples`: `["opencode", "claude", "codex", "antigravity"]`. Stays **not** an enum.
   - `x-import` (`:119`) unchanged: "anything else -> opencode" already covers it.
3. New `docs/local_agent_kit/conformance/manifests/runtime-engine-known-antigravity.json`:
   copy of `runtime-engine-known-codex.json` with `engine: "antigravity"` and
   description "A known engine preference (antigravity) is valid and silent."
   `expect: {errors: [], no_warnings: ["runtime.engine"]}`. Keep `contract_version: "1.5.0"`
   like the other runtime cases.
4. New `.../runtime-engine-antigravity-with-credential.json`: copy of
   `runtime-engine-codex-with-credential.json`, `engine: "antigravity"`,
   `credential: "google"`, description "`antigravity` runs under the user's own Google
   login; the credential is ignored with a warning." `expect: {errors: [], warnings: ["runtime.engine"]}`.
   `conformance/README.md` lists no cases, so it needs no edit.
5. `docs/local_agent_kit/guides/13-design-patterns.md:413`: "(`opencode`, `claude`,
   `codex` or `antigravity`; …)". No `{{placeholder}}` (the sync refuses one in this guide).
6. `docs/local_agent_kit/CHANGELOG.md`: new entry **above** `## 1.10.0`, unnumbered,
   modelled on "## Google Meet credential types" (`:189-196`):
   ```
   ## Antigravity engine

   No contract-version change. `runtime.engine` gains the known value `antigravity`
   (Google Antigravity's own ACP server, run by Cinna Desktop under the user's Google
   login) in `cinna-agent.schema.json`, `kit.py` and guide 13, with two conformance
   cases. Like `claude` and `codex` it spends no host credential, so `kit.py validate`
   warns when `runtime.credential` sits beside it, and it no longer reports the engine
   as unknown. The value was never invalid: the field is not an enum, and a host that
   does not run Antigravity runs the agent on OpenCode. Cloud import maps it to
   OpenCode, like `codex`.
   ```
   Invariant: the first **numbered** `## x.y.z` heading stays `1.10.0`
   (`backend/tests/unit/test_local_kit_conformance.py:131-143` checks exactly that; an
   unnumbered heading above it is fine). Do not touch the historical 1.5.0 notes
   (`:230-234`, `:314`).
7. Not changed, checked: `guides/11-go-cloud.md:145-148` ("any other value … picks
   OpenCode") is generic; `README.md`/`START.md`/`assistants/*` mention Codex as an
   authoring assistant, not an engine; `templates/agent/cinna-agent.json` carries
   `{{CONTRACT_VERSION}}`, unaffected by D1.

## §2 Phase 2 — backend comments, tests, feature docs

The cloud mapping already does the right thing: `sdk_for_engine_preference`
(`backend/app/services/environments/sdk_constants.py:159-185`) looks the stripped value
up in `MANIFEST_ENGINE_TO_SDK_ENGINE` (`claude`, `opencode` only) and falls back to
`MANIFEST_ENGINE_FALLBACK_SDK_ENGINE` (OpenCode). No code change.

1. `sdk_constants.py:149-152` comment: "Known manifest values today: `opencode`,
   `claude`, `codex`, `antigravity` — `codex` and `antigravity` have no cloud engine
   and so land on OpenCode." Docstring `:169-170`: "(including `codex` and `antigravity`)".
2. `backend/app/models/cli/account_convenience.py:51` comment: add `antigravity`.
3. `backend/tests/unit/test_sdk_engine_preference.py`
   - In the parametrize list at `:36-39` add `("antigravity", "claude-code/anthropic", "opencode/anthropic")`
     and `("antigravity", "opencode/google", "opencode/google")` (provider carried over).
   - `:56` engine list: add `"antigravity"`.
4. `backend/tests/unit/test_local_kit_tool.py`: one test beside the runtime tests
   (`~:2126-2180`) that validates a minimal folder with `runtime: {engine: "antigravity"}`
   → no warning containing "not a known engine"; with `credential: "google"` → exactly
   one warning containing "antigravity uses its own CLI login". Reuse the file's existing
   minimal-folder helper used by `test_runtime_credential_secret_lookalike_is_an_error`.
   (The conformance cases already pin the paths; this pins the wording the report asks
   to stay desktop-compatible.)
5. `docs/application/local_agent_kit/local_agent_kit_tech.md:682-686`: examples gain
   `antigravity`; "`claude`/`codex`/`antigravity` with a non-empty `runtime.credential` → warning".
6. `docs/application/cinna_cli_integration/account_cli_workspace.md:216-217`: "any other
   value (including `codex` and `antigravity`) → OpenCode".
7. Not changed, checked: `backend/tests/api/cli/test_account_cli.py:1928-1990` exercises
   the fallback end to end with `codex`; `antigravity` takes the identical code path, so
   the unit cases suffice. Catalog `engine` (`desktop_agent_revisions.py:260-280`,
   `git_repo_storage.py:332-350`) and `AccountAgentCreate.engine` are free strings.
   `DesktopAgentCard.tsx:111-164` renders the raw engine string. No frontend change.

## §3 Phase 3 — mirror sync and verification

1. Run only steps 3 and 4 of the sync (D3), from the project root:
   ```bash
   python3 -c "import importlib.util as u; s=u.spec_from_file_location('s','.cinna-core-kit/scripts/sync_platform_knowledge.py'); m=u.module_from_spec(s); s.loader.exec_module(m); m.sync_local_agent_kit(); m.sync_design_patterns_prompt()"
   cp docs/application/cinna_cli_integration/account_cli_workspace.md \
      backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/platform/application/cinna_cli_integration/account_cli_workspace.md
   ```
   `sync_local_agent_kit` clears and recopies `knowledge/local-kit/` verbatim (it skips
   `__pycache__`, denylisted suffixes); placeholders such as `{{KIT_VERSION}}` are
   copied **unrendered** and rendered per request by `LocalAgentKitService`
   (`kit_version` is a content hash computed without that token, so it moves by itself).
   `local_agent_kit_tech.md` is a `_tech` file and is never mirrored.
2. `git status` must show only: the Phase 1/2 files, the two new conformance files and
   their mirror copies, mirror copies of `kit.py`, `cinna-agent.schema.json`,
   `13-design-patterns.md`, `CHANGELOG.md`, `AGENT_DESIGN_PATTERNS.md`, and
   `platform/.../account_cli_workspace.md`. Anything else means the restricted sync was
   not used; revert it.
3. `diff -rq docs/local_agent_kit backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit`
   → only `__pycache__` lines.
4. Tests (in the container the kit tests fall back to the **mirror**, since `docs/` is
   not mounted, so they are only meaningful after step 1):
   ```bash
   docker compose exec backend python -m pytest tests/unit/test_local_kit_conformance.py tests/unit/test_local_kit_tool.py tests/unit/test_sdk_engine_preference.py tests/api/cli/test_local_agent_kit.py -v
   ```
   Expect the two new parametrized conformance cases, the coherence test still on
   `1.10.0`, and `runtime-engine-unknown.json` still warning (unknown engine → OpenCode).
5. `make check-docs` (no frontmatter changed; catches citation drift).

### Runtime / rebuild implications (no action, record in the commit message)

- `/agent-start` and the account context package read the backend's own copy of
  `knowledge/local-kit/`. Dev compose mounts `./backend/app`, and the snapshot cache is
  keyed on newest mtime + file count, so the change is live without restart. Prod needs
  the normal backend image rebuild (`COPY ./app`).
- `AGENT_DESIGN_PATTERNS.md` is baked into agent images from `app_core_base`; running
  cloud environments keep the old guide line until their environment is rebuilt. A
  one-word difference in a building prompt; no forced rebuild.
- Nothing under `protocols/` or `skills/` changes, so `platform_protocol_files.py`
  consumers are unaffected.

## §4 Why no contract bump (D1)

- The CHANGELOG rule (`:14-17`): major = breaking, minor = additive. Contract version
  answers "may this tool operate this folder?" (`docs/application/local_agent_kit/local_agent_kit.md:61-68`).
  A folder naming `antigravity` was already valid under every 1.x tool (not an enum,
  warning only), so no tool's answer changes.
- Precedent in the same file: "## Google Meet credential types" (`:189-196`) added known
  values to `cinna-agent.schema.json` and `kit.py` with "No contract-version change";
  `codex` itself was added by the desktop without its own number (1.5.0 note, `:314`).
- A bump has a real cost: every newly scaffolded folder would be stamped `1.11.0`, and a
  push of such a folder to a core still on 1.10.0 prints the minor-newer warning
  (`backend/app/services/library/push_policy.py:383-389`) for a change that alters no
  folder.
- The conformance set ships in the contract bundle and grows by two cases, but it travels
  with its kit (the desktop re-syncs the bundle with `make kit-sync`); the desktop's
  validator already passes both cases, and its bundle tests read the version from the
  bundled file (`agentsHomeService.test.ts:39`), with no pin.

## §5 Report claims: verified / wrong

Verified:
- `kit.py:226-228` lacks `antigravity`; comment `~:1622`; CLI-login warning wording
  matches the desktop's.
- Schema `runtime.engine` at `:114-120`, not an enum; examples lack `antigravity`.
- Codex conformance pair exists; `conformance/README.md` does not list cases.
- Guide 13 `:413`; `sdk_constants.py:147-175` falls through to OpenCode via the generic
  path; codex cases exist in `test_sdk_engine_preference.py`; `account_convenience.py:51`.
- Mirror is produced by `.cinna-core-kit/scripts/sync_platform_knowledge.py`, and is in
  step with `docs/local_agent_kit/` today.
- No engine list in `backend/app` beyond the two comments; `MarketplaceType`/addons
  "codex" are the plugin format; tasks protocol is engine-agnostic.

Wrong or incomplete:
- **Conformance "known" case**: report says `expect: {errors: [], warnings: []}`.
  `warnings: []` checks nothing (`warnings` means "each listed path is present"); the
  codex model uses `no_warnings: ["runtime.engine"]`. Use that.
- **Changelog rule**: report frames a minor bump as likely; the file's own precedent
  (Google Meet types) says no bump (§4).
- **Missed places**: guide 13 is also copied into
  `app_core_base/core/prompts/AGENT_DESIGN_PATTERNS.md` by the same sync (step 4);
  `docs/application/local_agent_kit/local_agent_kit_tech.md:682-686` and
  `docs/application/cinna_cli_integration/account_cli_workspace.md:216-217` (plus its
  `platform/` mirror) name the engine set.
- **"Update the mirror the way this repo keeps them in step"**: the full sync would also
  sweep unrelated `knowledge/platform/` drift into this change (D3).

## §6 Deferred / other repo

- `docs/local_agent_kit/assistants/antigravity.md` (D2).
- `../cinna-desktop`: `make kit-sync CORE=<this repo>`, then `contractBundle`/`npm test`
  and the after-sync checklist in `docs/agents/local_agents/kit_contract_tech.md`. With
  D1 the bundle's `CONTRACT_VERSION` stays `1.10.0`.
- Separate commit: full `make sync-platform-knowledge` to clear the existing
  `knowledge/platform/` drift (D3).
- If the user picks a minor bump instead: `CONTRACT_VERSION`, `kit.json:3`,
  `layout.json:2`, a `## 1.11.0 — antigravity engine` heading replacing the unnumbered
  one, `backend/tests/unit/test_local_kit_conformance.py:135`,
  `docs/application/local_agent_kit/local_agent_kit.md:64` (+ its `platform/` mirror),
  then the same restricted sync.
- `docs/local_agent_kit/protocols/tasks.schema.json`: the top-level description lists
  the tool names each engine sees (Claude Code, Codex, OpenCode) but not Antigravity.
  How Antigravity names those tools is a desktop-side question; leave the schema
  unchanged until the desktop settles it.

## §7 Decisions for the user

1. D1: no contract bump (recommended) vs minor bump to 1.11.0 (file list in §6).
2. D2: write `assistants/antigravity.md` now, or leave deferred (recommended).
3. D3: clear the unrelated `knowledge/platform/` mirror drift in a separate commit
   (recommended) or fold it into this one.
