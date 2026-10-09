# Team sharing (core) — progress

Spec: cinna-desktop/plans/team-sharing/{README,core}.md. Scope this run: P0 + P2 + admin Team library page. P5 (sign-off gate) and P7 deferred.

- 2026-10-09 kickoff: tree clean except cinna.log (unrelated, leave). Planner launched (a453dc99eac289b52) -> docs/plans/team_sharing_plan.md.
- 2026-10-09 plan written (240 lines, D1–D13, phases A–E). Defaults taken for §15 Q1–Q5. Launched dev-ab (phases A+B) and ui-design (design mode §11) in parallel.
- 2026-10-09 resume: first dev-ab/ui-design produced nothing (stopped at early handback). Relaunched dev A+B (aad590ec76455e5b3) and ui-design (ad2e98fa9b8efe2d0).
- 2026-10-09 UI Specification present at end of plan (first ui-design wrote it; second corrected in place: userRoleLabel, Maintainer only for person grants; 5 API questions in spec). Old dev-ab possibly still editing A — told to stop; new dev warned.
- 2026-10-09 Phases A+B developed (aad590ec76455e5b3 + partial dev-ab). Migration 0e62eb9df5d4 (down 49c748e2c50a), applied. Narrow checks 200+133 passed. Next: review A/B, tests A/B, dev C+D in parallel.
- 2026-10-09 fan-out: reviewer A/B (aa0507c815d70e995), test-writer A/B (a61c31a6e813edc07), dev C+D (a908ee1176f250b64).
- 2026-10-09 review A/B (aa0507c815d70e995): CHANGES REQUIRED — 1 blocking (v3 export baseline/excluded files), 3 should, 4 nits. Relayed to dev A/B (aad590ec76455e5b3, resumed); test-writer told.
- 2026-10-09 tests A/B (a61c31a6e813edc07): unit revision_format/conformance/library_models_storage, api agents/git/agents_git_tree_v3_test, migrations/git_repo_library_test. 80/1 (transient mid-edit), rerun green. Gaps: baseline-after-export assertion, concurrent replace_grants.
- 2026-10-09 A/B fixes 1-8 landed (aad590ec76455e5b3); checks 257/10skip. Gap: kit-imported agent dirty after v3 push. Re-review of fix diff sent to reviewer.
- 2026-10-09 re-review A/B: 2-8 OK; 1 still blocking (live-side projection), +should settings_dirty prompts, nit committed-tree baseline. Relayed to dev A/B.
- 2026-10-09 C+D developed (a908ee1176f250b64): git_http, push_policy, hook, library routes/service/events, deps, nginx, compose, client regen. Git smoke green. Fan-out: review C/D (a7a80d251dcb8e75b), tests C/D (a7b005a456dcaf668), ui-dev E (a103bad9a3ae657fd). A/B round-2 fixes in progress (aad590ec76455e5b3).
- 2026-10-09 A/B round-2 fixes landed (live-side v3 projection, settings baseline); 52 passed. Sent to reviewer for fix-diff; test-writer A/B adding dirty-clean-after-v3-push tests.
- 2026-10-09 review C/D (a7a80d251dcb8e75b): CHANGES REQUIRED — 1 blocking (hook missing ⇒ policy off), 5 should, 3 nits. Relayed to dev C/D (a908ee1176f250b64, resumed); test-writer C/D told.
- 2026-10-09 A/B APPROVED (round-2 re-review, aa0507c815d70e995). Phases A, B done+approved.
- 2026-10-09 A/B tests: +3 dirty-clean-after-v3-push tests; tree_v3 file 5 + revision_format 24 = 29 passed.
- 2026-10-09 C/D fixes 1-9 landed (a908ee1176f250b64), smoke green; #9 placed in GitHttpService.authorize. Re-review sent.
- 2026-10-09 documenter launched.
- 2026-10-09 C/D APPROVED (re-review a7a80d251dcb8e75b). Backend phases A-D done+approved.
- 2026-10-09 docs done (a2f3685de7c24773a): team_library{,_tech}.md, data_volumes.md, +8 updated; docs check green. Pending: make sync-platform-knowledge after docs settle.
- 2026-10-09 tests C/D (a7b005a456dcaf668): tests/api/library/ 32 passed; one unreproduced hook decline flake. Regression runner launched.
- 2026-10-09 Phase E built (a103bad9a3ae657fd): ui review PASS round 2, 10/10 all surfaces; tsc/biome clean.
- 2026-10-09 FE code review (a38f2b7e70b0b8837): APPROVE w/ 1 warning (stale grants form) + 4 suggestions; relayed to ui-dev.
- 2026-10-09 platform-knowledge mirror synced; docs check green.
- 2026-10-09 FE review fixes 1-5 landed (gcTime 0, queryKeys.ts, 404 close, cast, button trigger); tsc/biome clean. Phase E done.
- 2026-10-09 regression green (a0a3bc979a9c0ec16): library 32x3, agents/git 31, cli 189, unit 2954/11skip, migrations 14, infra 4. Run complete; not committed.
