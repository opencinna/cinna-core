# Backend Test Suite Refactor Plan

**Status:** Complete (all phases)
**Created:** 2026-06-10
**Source:** Full-suite audit (~100k lines, ~30 domains) against three criteria: isolation (python-only + local test DB), no heavy setup where unnecessary, scenario/end-to-end focus over micro-unit tests.

## How to execute this plan

- Phases are executed **sequentially, one agent per phase**. Each phase ends with a verification run (listed per phase). Do not start a phase until the previous one is green.
- Before any test work, read `backend/tests/README.md` and any domain README (`tests/api/agents/README.md`, `tests/api/mcp_integration/README.md`, `tests/api/a2a_integration/README.md`).
- Tests run inside Docker: `docker compose exec backend python -m pytest <path> -v`. Do NOT run the full suite per phase unless the phase says so — run the affected directories only.
- Update the **Status** line above and the per-phase checkbox when a phase completes.
- Rule of thumb everywhere: when a test needs a workaround, the source code is wrong — fix the source, never the test (see "Source Code Invariants" in `backend/tests/README.md`).

## Reference-quality files (patterns to copy, do not regress)

`tests/api/mcp_integration/test_mcp_connector_direct_tokens.py`, `tests/api/desktop_auth/test_desktop_auth.py`, `tests/api/app_sync/test_app_sync.py`, `tests/api/identity/test_identity.py`, `tests/api/dashboards/test_dashboards.py`, `tests/api/agents/agents_team_task_delegation_test.py`, `tests/api/app_mcp/app_mcp_oauth_flow_test.py`.

Justified-exception pattern: direct DB access / direct service invocation is acceptable ONLY with a docstring explaining why no API path exists (e.g. backdating token expiry, injecting wrong-owner revision specs, MCP protocol handlers that have no TestClient surface).

---

## Phase 1 — Conftest infrastructure: per-test app lifespan, scheduler isolation, fixture cost

- [x] Done

**Problem.** `tests/conftest.py:99` enters `TestClient(app)` per test, so every test runs the full `app/main.py` lifespan: starts/stops ~13 APScheduler BackgroundSchedulers, registers ~30 event handlers, spins up the MCP registry. Worse, scheduler jobs bind to the **real** `app.core.db.engine` (the `app` DB, not `app_test`) — e.g. `app/services/files/file_cleanup_scheduler.py:16 Session(engine)` — so a job firing mid-run mutates the dev application database from inside tests (isolation escape).

**Changes.**
1. Gate scheduler startup (and any other heavy background startup that tests never need) behind a settings flag, e.g. `settings.DISABLE_BACKGROUND_SCHEDULERS` or detect test mode (`TEST_DB_SERVER` env present + an explicit `TESTING` flag set by conftest). Prefer an explicit `TESTING: bool = False` setting that `tests/conftest.py` sets before app import. Schedulers must not start when set. Inventory every `scheduler.start()` / BackgroundScheduler instantiation in `app/main.py` lifespan and services it calls.
2. Make `superuser_token_headers` session-scoped (JWT is stateless, superuser seeded once per session outside the rollback transaction) — `tests/conftest.py:104-106`. Keep `normal_user_token_headers` function-scoped only if it must be (the user it creates is rolled back per test — check; if so it stays function-scoped).
3. Make `setup_default_credentials` (`tests/utils/fixtures.py:77-80`) opt-in or lazy instead of autouse-taxing every test in 15 domains. Approach: keep the fixture name/API stable; have domain conftests opt in only for files that need it, OR make it lazy (create on first use). Choose the least-churn option that removes the cost from pure-CRUD tests.
4. `tests/api/credentials/conftest.py:26-59` and `tests/api/ai_credentials/conftest.py:19-47`: heavy autouse stub stacks (env adapter, background tasks, external services) apply to every file in the dir, but `test_credentials.py`, `test_credentials_sharing.py`, `test_ai_credentials.py` (~66 pure-CRUD tests) never touch agents. Scope the heavy fixtures to the files that need them (module-level fixtures or conditional autouse via request.module checks — pick the cleanest).

**Verify.** Run `tests/api/credentials/`, `tests/api/ai_credentials/`, `tests/api/auth/`, `tests/api/users/`, `tests/api/agents/agents_create_flow_test.py`, then `tests/api/agents/` as the broad regression. Time `tests/api/credentials/` before and after; report the delta. Confirm no scheduler threads start during a test run (grep startup logs).

**Risk.** Touches `app/main.py` startup — if the flag accidentally affects production paths, envs break. The flag must default to current behavior (schedulers ON) and only conftest sets it.

---

## Phase 2 — Architecture drift test for patch-target lists

- [x] Done

**Problem.** `tests/utils/fixtures.py` `CREATE_SESSION_TARGETS_AGENT` (~lines 38-45) plus per-domain extensions cover ~14 modules, but ~31 app modules import `create_session`. Unlisted importers silently open sessions on the real engine during tests (the "handler silently returns" failure mode in the README). Known missing: `app/services/email/sending_service.py`, `app/services/agents/agent_status_service.py`, `app/services/agents/cli_commands_service.py`, `app/mcp/tools.py`, several `app/services/agents/commands/*`. Same drift in `BACKGROUND_TASK_TARGETS_FULL` (missing `agent_scheduler_service`, `email/processing_service`, `rebuild_env_command`).

**Changes.**
1. New test in `tests/architecture/` (pattern: `channel_ingestion_callers_test.py`) that scans `backend/app/` for modules importing `create_session` (and `create_task_with_error_logging`) and asserts each appears in the union of the relevant target lists in `tests/utils/fixtures.py`. Allowlist for intentional exclusions, each with a comment explaining why.
2. Fix the lists: add the missing targets discovered by the new test. After extending the lists, the affected domain suites must still pass (the patches now actually reach those modules — may surface latent test issues; fix them properly, not by removing targets).
3. While in `tests/architecture/`: fix stale failure text in `channel_ingestion_callers_test.py:136-138` (references a removed Phase-1 debug route).

**Verify.** Run `tests/architecture/`, then `tests/api/agents/`, `tests/api/input_tasks/` (the domains most sensitive to create_session patching).

---

## Phase 3 — Fix the three source bugs tests are working around, then de-mock the tests

- [x] Done

**3a. Webhook public routes use `Session(engine)`** — admitted at `tests/api/agents/agents_webhooks_test.py:19, 419, 873`. The route/service (`app/api/routes/agent_hooks.py` / `AgentWebhookService`) must use `create_session()` so test transactions can see it (README invariant #1). Then rewrite `agents_webhooks_test.py`:
   - Remove the 17 patches of `app.api.routes.agent_hooks.AgentWebhookService.{validate_webhook_token,fire_webhook}` — hit the real public endpoint.
   - Delete the self-verifying mock at lines 436-465 (`_reject_old_token`); after rotation, call the real endpoint with old token (expect rejection) and new token (expect success).
   - `test_two_concurrent_fires_produce_independent_logs` (~:1240) must assert real log rows via API, not mock-returned IDs.

**3b. `activate_suspended_environment()` writes via fresh `Session(engine)`** — see workaround comment at `tests/api/cli/test_cli.py:326-336` (assertion weakened to "not 404/409"). Fix the service to use `create_session()`, then strengthen the test to assert the environment actually transitions.

**3c. Legacy `update_status()` bypasses transition validation** — surfaced by `tests/api/input_tasks/test_task_status_transitions.py:99-105` ("archive again may return 200 or 400, both acceptable"). Investigate the legacy route; either route it through `update_task_status()` validation or document the intended semantics — then pin the test to ONE expected status code.

**Verify.** Run `tests/api/agents/agents_webhooks_test.py`, then `tests/api/agents/`; `tests/api/cli/`; `tests/api/input_tasks/`. These are source changes — also run `tests/api/external/` and `tests/api/a2a_integration/` as blast-radius checks for 3a/3b.

---

## Phase 4 — Kill vacuous / self-verifying / dead tests

- [x] Done

Each item: make the test assert the real behavior, or delete it if redundant.

1. `tests/api/agent_environments/test_env_console.py:341-354, 385-395, 459-470` — "dep now passes" phases are `try: connect ... except Exception: pass` with no assertion. Assert success explicitly (e.g. the patched service mock was reached / WS handshake completed). Rejection phases: replace bare `pytest.raises(Exception)` (incl. `match=""` at :179) with `WebSocketDisconnect` + assert close code (1008 / 4404).
2. `tests/api/agents/agents_bundles_scheduler_propagation_test.py:470` — `assert X == Y or True` is always true. Assert the actual cron value.
3. `tests/api/agents/agents_backfill_router_trigger_test.py:168-172` — tautological disjunct; tighten to the real assertion.
4. `tests/api/agents/agents_webapp_test.py:917-927` — `assert r.status_code in (200, 503)` with a comment admitting it can't tell which path fired. Set env status deterministically (via stub) and assert one outcome.
5. `tests/api/agents/agents_email_task_integration_test.py:277-285` — `if status != completed: force-complete via InputTaskService` masks the regression under test. Replace with hard `assert task["status"] == "completed"` via API. If it fails, that's a real bug to investigate (likely create_session patching — Phase 2 may have fixed it).
6. `tests/api/agents/agents_bundles_install_readiness_test.py:484-505` — self-healing precondition (recreates a missing `AICredentialShare`). Assert the share exists instead.
7. `tests/api/users/test_mfa_totp_login.py:85-115` — Phase 3 "verify TOTP" is comments only; implement it using `enroll_totp_and_get_secret` (`tests/utils/mfa.py:454`).
8. `tests/api/users/test_mfa_passkeys.py:468` — `test_google_oauth_mfa_branch` accepts 400 or 501; pin to the real expected behavior or delete.
9. `tests/api/cli/test_cli_removed_endpoints.py:46-86` — accepting 401 proves nothing. Assert against the FastAPI route table (path absence) or call with a valid CLI token and require 404/405.
10. `tests/api/agents/agents_resilient_plugins_test.py:356, 505-507` — bare `return` skips ("no env to sync") let tests pass without asserting. Make the precondition a hard assertion. Also delete the dead `LLMPluginService` import at :433 and the stale rule-violation comment at :457 (the test now uses adapter capture).
11. `tests/api/agents/agents_non_llm_bridge_test.py:497-509` — handler-registry test tolerates `None` for `/files`; assert registration or delete.
12. `tests/api/agents/agents_bundles_credential_specs_test.py:304-331` — stale "documented bug" workaround: the `_encrypt_data` bug is fixed (asserted at `agents_bundles_install_credentials_test.py:211`); restore the omitted placeholder-visibility assertion, delete the stale paragraph.
13. `tests/api/agents/agents_cli_commands_test.py:195-217` — outcome forced by patching `CLICommandsService.get_cached_commands` (the thing under test). Make the adapter-stub fetch path deterministic and assert the real fetch→cache→endpoint flow, or re-scope/rename the test honestly. Also wrap the `EnvironmentTestAdapter.workspace_files` class-attribute mutation (:88, :185) in a fixture.
14. `tests/api/app_sync/test_pairing_hardened.py:520-553` — TTL expiry "tested manually via curl" + near-no-op config sanity test. Patch `APP_SYNC_PAIRING_TTL_SECONDS=0` BEFORE `pairing_start` (born-expired row; same technique as `test_desktop_auth.py:1391`), assert 410, delete the sanity placeholder.

**Verify.** Run each touched file, then the touched domain dirs: `tests/api/agent_environments/`, `tests/api/agents/`, `tests/api/users/`, `tests/api/cli/`, `tests/api/app_sync/`.

---

## Phase 5 — Isolation leaks

- [x] Done

1. `tests/api/auth/test_login.py:44-57` — password-recovery test patches only `SMTP_HOST`/`SMTP_USER` settings; `UserService.recover_password` → `send_email` opens a REAL connection to `smtp.example.com` (passes only because the `emails` lib swallows failures; hangs offline). Patch `send_email` at its import site in the user service module.
2. `tests/api/users/users_roles_test.py:220-330` — creates real agents with NO environment stubs (`tests/api/users/` has no conftest), so `EnvironmentService.create_environment(auto_start=True)` schedules a real Docker lifecycle build. Add `tests/api/users/conftest.py` mirroring the stub fixtures used by `tests/api/credentials/conftest.py` (env adapter + background tasks) for agent-creating tests.
3. `tests/api/mcp_integration/test_mcp_file_upload.py:192` — real `time.sleep(1)` to expire a JWT; mint an already-expired token (negative expiry) or patch the clock. Also: :278-287 mutates `settings.UPLOAD_MAX_FILE_SIZE_MB` by raw assignment — use `unittest.mock.patch`.
4. MFA in-memory rate-limit buckets (`_verify_rate_limit_log`, `_anonymous_verify_rate_limit_log` in the MFA service) survive rollback. Only `test_mfa_trusted_device.py:72-92` clears them (autouse); `test_mfa_totp_login.py:460-498` clears one bucket manually, not in `finally`. Promote the clearing fixture to the new `tests/api/users/conftest.py` (from item 2) so all MFA files are order-independent.
5. `tests/unit/test_claude_code_event_transformer.py:~84` — unconditional `sys.modules["claude_agent_sdk"] = MagicMock()` at import time leaks process-wide. Use `sys.modules.setdefault` (pattern: `tests/unit/test_opencode_mcp_bridge.py:392`) or fixture-scoped patching.
6. `tests/api/users/test_mfa_totp_login.py:209-300` — wall-clock flake if a 30s TOTP step boundary is crossed; compute the verification code from the same timestamp used at enrollment.

**Verify.** Run `tests/api/auth/`, `tests/api/users/`, `tests/api/mcp_integration/test_mcp_file_upload.py`, `tests/unit/`.

---

## Phase 6 — Document tests/unit/ policy; relocate misplaced unit tests out of api/

- [x] Done

**6a. README updates (`backend/tests/README.md`).**
- Add a "Unit tests (`tests/unit/`)" section: when unit placement is allowed (pure logic, transformers, parsers, decision tables; no DB/TestClient; service imports allowed THERE), how it relates to Rule 1 (rule applies to `tests/api/` only), and the cross-reference convention (api file points to unit file and vice versa).
- Document `tests/architecture/` (contract/drift tests).
- Update the directory-structure listing (currently shows 5 of ~26 api domains; `items/` listed but `test_items.py` no longer exists — remove or note).
- Replace `tests/unit/README_IN_PROGRESS.md` (stale work journal) with a proper short `tests/unit/README.md`.

**6b. Relocations (move + adapt imports; these are good tests in the wrong place).**
- `tests/api/mcp_integration/test_a2a_connector_oauth_dcr.py` — move the ~15 pure unit tests (egress guard `validate_external_endpoint_url`/`is_host_blocked` at :114-303; OAuth private methods `_put_state`/`_take_state`/`_generate_pkce`/`_apply_token_response`; `MCPProviderService._derive_lifecycle_state` at :742-794) to `tests/unit/test_mcp_provider_oauth.py` + `tests/unit/test_egress_guard.py`. Keep API-level flows in place. Drop the egress-guard cases already covered end-to-end by `test_a2a_connector_consumer.py:698-757` (keep DNS-resolution cases only). Also remove the banned `from app.core import security` import (:42) — the connected-credential state at :338, 400, 510, 598 is reachable via `POST /connect/external` + the mocked `/oauth/callback` route.
- `tests/api/mcp_integration/test_mcp_resources.py:130-318` — move the ~25 private-function tests (`_parse_workspace_uri`, `_logical_to_disk_path`, `_guess_mime_type`, `_collect_files_from_tree`) to `tests/unit/test_mcp_resources_helpers.py`; keep adapter-backed read/list tests in api/.
- `tests/api/mcp_integration/test_mcp_notifications.py` — whole file is MagicMock unit tests; move to `tests/unit/`.
- `tests/api/mcp_integration/test_mcp_prompts.py` — move the `_parse_prompt_line` half to `tests/unit/`; keep handler tests.
- `tests/api/agents/agents_bundles_plugin_propagation_test.py` — whole file is service-level (no TestClient); move to `tests/unit/test_plugin_sync_propagation.py` (or a service-test home defined in 6a). Note in the file that merge scenarios are also reachable via publish→install→apply-update API flow (future coverage).
- Private-helper tests scattered in api files → `tests/unit/`: `test_extract_attachments_unit` (`agents_message_attachments_test.py:1289`), `test_extract_webapp_actions_unit` (`agents_webapp_chat_actions_test.py:491` + module-level private import at :28), `_assemble_session_prompt`/`filter_headers` block (`agents_webhooks_test.py:1090-1410`), `test_include_in_llm_context_attributes_are_set_correctly` (`agents_non_llm_bridge_test.py:466`), `AppAgentRouteService._tokens_for_similarity`/`_jaccard_similarity` (`app_mcp_auto_managed_route_test.py:433-485`), environment exception classes (`test_agent_environments.py:421` — consider deleting, 403/404 covered by scenarios), env-console tracker rate/concurrency caps (`test_env_console.py:476, 521`).
- `tests/api/mcp_integration/test_a2a_connector_consumer.py:842-864` — constant assertions (`AGENT_ENV_ALLOWED_FIELDS`, `SENSITIVE_FIELDS`) → `tests/unit/`; dedupe with the inline copy at :247-249 (keep one). Delete dead imports at :464-467 (`asyncio`, `contextmanager`, `NonClosingSessionProxy`, `patch as mp`) and the unused `db` param.
- `tests/api/agents/agents_bundles_install_readiness_test.py` — scenarios A/B/D/F/G call `InstallReadinessGate.check(db, install)` directly (:50-54, 233-558) though `GET /agents/{id}/setup-status` exposes the same data (tested in scenarios H+). Rewrite A/B/D/F/G against the endpoint; move MagicMock defensive-branch tests C (:292) and E (:381) to `tests/unit/`.
- `tests/api/agents/agents_bundles_credential_specs_test.py:630-697` — scenario I white-box duplicates `agents_bundles_publish_settings_test.py:377`'s end-to-end coverage; delete it.
- Misplaced file: `tests/api/auth/test_ai_functions_sdk.py` tests `/users/me` preferences → move to `tests/api/users/`.

**Verify.** Run `tests/unit/`, `tests/api/mcp_integration/`, `tests/api/agents/`, `tests/api/app_mcp/`, `tests/api/users/`, `tests/api/auth/`.

---

## Phase 7 — Shared helper extraction (dedupe)

- [x] Done

1. **`tests/utils/bundle.py`** (biggest win, ~1.5k duplicated lines): 15 of 18 `tests/api/agents/agents_bundles_*` files re-define near-identical `_make_user_and_headers`, `_publish`, `_make_public`, `_install`, `_create_credential`, `_link_credential_to_agent` (some shadow existing `tests/utils/credential.py` helpers). Create `publish_bundle(...) -> (bundle_id, bundle_uuid)`, `install_bundle(...)`, `make_developer_user(...)` etc.; migrate the bundle test files to use them. Mechanical but large — keep behavior identical, migrate file by file, run `tests/api/agents/agents_bundles_*` after each batch.
2. **A2A SSE helpers**: `_extract_parts_from_sse_event`/`_part_text`/`_part_metadata` duplicated in `test_a2a_content_kind_metadata.py:54-68` and `test_a2a_tool_result_streaming.py:55-69`; `_extract_task_id` in 3 external files (`external_a2a_route_test.py:133`, `external_sessions_test.py:266`, `external_a2a_identity_test.py:136`); `_a2a_headers` in 3 places → move all to `tests/utils/a2a.py`.
3. **Desktop OAuth token dance**: promote `_obtain_tokens` (`test_desktop_auth.py:1288-1302`) to `tests/utils/desktop_auth.py` as `obtain_desktop_tokens()`; replace the inline 16× `get_authorization_code` / 21× `list_desktop_clients` repetitions and the re-implementation at `external_sessions_test.py:814-826`.
4. **Env-console JWT minting** (`test_env_console.py:46` imports banned `app.core.security.create_access_token`): wrap token-minting in a `tests/utils/` helper with a documented exemption (mirrors `tests/utils/cli.py`).

**Verify.** Run `tests/api/agents/` (bundles subset first: `tests/api/agents/agents_bundles_*`), `tests/api/a2a_integration/`, `tests/api/external/`, `tests/api/desktop_auth/`, `tests/api/agent_environments/test_env_console.py`.

---

## Phase 8 — Replace lazy direct-DB access with existing APIs

- [x] Done

Keep the documented-justified DB usages (do NOT touch: token backdating in `test_desktop_auth.py:1455, 1516`, revision-spec injection `agents_bundles_install_credentials_test.py:532-546`, regression binding insert in `external_a2a_identity_test.py:~604`, email queue reads sanctioned by agents README, `backfill(db)` script invocation, knowledge_query embedding inserts).

1. `CredentialShare` direct inserts claiming "no API" in 4 files — `POST /credentials/{credential_id}/shares` exists (`app/api/routes/credential_shares.py:45`): `agents_bundles_install_context_test.py:160-182`, `agents_bundles_install_credential_match_test.py:207-228`, `agents_bundles_per_user_scope_test.py:150-171`, `agents_bundles_service_uri_test.py:~165`. Where the test deliberately shares an `allow_sharing=False` credential (bypassing the API guard), keep the DB insert but say so explicitly in the docstring.
2. `agents_bundles_install_credentials_test.py:377-383` (+ `agents_bundles_install_readiness_test.py:360-364`) — `allow_sharing` flip via DB; `PATCH /credentials/{id}/sharing` exists (`credential_shares.py:129`).
3. `external_a2a_identity_test.py:353-361, 511-519` — `IdentityBindingAssignment.is_enabled` flips via DB; `toggle_identity_contact` helper is imported at :38 and unused. Use it. Also fix the no-op assertion at :319-324 (assert session count via owner's session list API).
4. `tests/api/mcp_integration/test_mcp_file_upload.py:153-157, 305-309` — env-status flips via `db.get/add/flush`; use the env adapter stub/lifecycle API or a shared `tests/utils/environment.py` helper.
5. `tests/api/input_tasks/test_task_agent_api.py:716-723` — clears `task.session_id` via DB (and `db.commit()` mid-test); re-execute via `POST /tasks/{id}/execute` with a placeholder stub instead.
6. `tests/api/ai_credentials/test_ai_credentials_propagation.py:13-14, 171-187` — imports `AgentEnvironment` + `EnvironmentService`, mutates env rows. Move the "link credential to env" poke behind a `tests/utils/` helper with an explicit TODO for the missing API seam (or use an existing env-link endpoint if one exists — check `app/api/routes/` first).
7. `agents_agent_api_test.py:804-819 (also 871, 921, 985, 1023)` — five duplicated `agent_api_policy_cache` DB-write blocks guarded by `if env:` (silent no-op risk). Extract `_set_policy_cache` helper that asserts the env exists; prefer exposing policy via the adapter stub if cheap.
8. Lazy DB verification where APIs exist: `agents_bundles_install_context_test.py:743-759, 809-828, 899-912, 967-977` (use `GET /agents/{id}/credentials`, `CredentialPublic.is_placeholder`, agents list); `agents_bundles_install_readiness_test.py:809-816, 856-868` (sessions API); `agents_bundles_template_sharing_test.py` partial (use `GET /credentials/{id}`); `agents_bundles_pbp_agent_api_test.py:297, 329` (assert via adapter-captured payload instead of `CredentialsService.prepare_credentials_for_environment` + private-attr monkeypatch; pattern: `agents_resilient_plugins_test.py` manifest capture). Same for `agents_agent_api_test.py:1324`.
9. Cosmetic: remove unused `db: Session` params + `sqlmodel` imports: `test_a2a_files_command.py:18,73`, `test_service_account_credential.py:14`, `test_ssh_key_credential_env_sync.py:17`, `test_ssh_key_credential_update_share.py:22`, `app_data_test.py` (test 1 signature).
10. `external_sessions_test.py:33` — banned `from app.core import security` for `ALGORITHM` only; decode the JWT with `jwt.decode(..., options={"verify_signature": False})`.

**Verify.** Run `tests/api/agents/agents_bundles_*`, `tests/api/external/`, `tests/api/mcp_integration/`, `tests/api/input_tasks/`, `tests/api/ai_credentials/`, `tests/api/credentials/`, `tests/api/a2a_integration/`.

---

## Phase 9 — Final regression + docs

- [x] Done — full suite: 2180 passed in 15:06 (exit 0), 2026-06-10

1. Run the FULL backend suite (`docker compose exec backend python -m pytest tests/ -q`). Fix any stragglers.
2. Update `backend/tests/README.md` once more if any conventions changed in phases 1-8 (e.g. new utils, opt-in fixtures, TESTING flag).
3. Update this plan's status to Complete.

---

## Deferred (explicitly NOT in this plan)

- **Pre-convention micro-unit consolidation** of old CRUD files (`test_credentials.py` 20 tests, `test_credentials_sharing.py` 21, `test_ai_credentials.py` 25, `auth/test_users.py` 26, `ssh_keys` 17, `workspaces` 22, guest-shares files, `agent_schedule_types_test.py`, `test_mcp_oauth_flow.py` 24 agent provisions, `agentic_teams` handover block ~18 agent provisions): folding into scenarios. Worth doing when each domain is next touched; consolidation now would create churn without behavior change.
- Restoring `items/` test coverage (file is gone; decide whether items feature still warrants tests).
- One end-to-end notification scenario driving the error→email path through a real `StubAgentEnvConnector` error stream (`notifications/test_notification_settings.py` scenarios 4-9 currently call services directly).
