# Local Agent Kit → Cinna Desktop contract: requirements and settled decisions

**Status:** requirements brief, decisions settled. The implementation plan is a separate
file (`local_agent_kit_desktop_contract_plan.md`); this file is the input to it and the
answer we owe the desktop team.

**Source request:** `/Users/evgenyl/dev/ml-llm/cinna-desktop/docs/agents/local_agents/cinna_core_handover.md`
(573 lines, read it — this brief does not restate it). Section numbers below are that
document's. The desktop's *authored candidate contract* is at
`/Users/evgenyl/dev/ml-llm/cinna-desktop/resources/cinna-kit-contract/` — a proposal, not
a published artefact.

**What we own:** `docs/local_agent_kit/` (the kit SSOT), `backend/app/api/routes/local_agent_kit.py`,
`backend/app/services/cli/local_agent_kit_service.py`, `backend/app/api/routes/cli.py`
(account-CLI surface), `backend/app/api/routes/desktop_auth.py` +
`backend/app/services/desktop_auth/`. `cinna-cli` is a separate repo
(`/Users/evgenyl/dev/ml-llm/cinna-cli`) and is **out of scope** — its follow-ups are
listed in §7.

---

## 1. Scope decision

**In scope** (user-approved):

- Handover §2 — contract / guides split, `layout.json`, contract tarball + version endpoint (§8.1).
- Handover §3 — manifest schema changes.
- Handover §4 — versioning rules and the compatibility gate.
- Handover §5 — `kit.py` changes (`new --description/--json`, `chat`, `list` DESKTOP column,
  `validate` gate + new field checks, `export` from `layout.json` + `content_hash`, `refresh`).
- Handover §6 — template and guide changes.
- Handover §7 — default workshop path.
- Handover §9 — conformance surface (the two `kit.py` affordances that make the checks runnable).
- Handover §8.2 — `POST /api/v1/cli/account/desktop-token`.

**Out of scope, deliberately:**

- §8.3 server-side import changes and §8.4 "account-CLI endpoints accept desktop tokens".
  Consequence, and it must be stated in the reply to the desktop team: with §8.2 but
  without §8.4, the **silent link works and Publish does not**. The desktop's degraded
  path from §8.4 ("run `cinna agent import` from `Cloud/<host>/`") is the shipping path
  for now. Everything the desktop needs to *detect* publication state (`publications[]`,
  `content_hash`) still lands — but **not in the manifest**: the ledger lands in the
  sibling `publications.json` at the agent root, and the hash in `kit.py`. See D11's
  amendment for why it moved. The scope decision above is unchanged; only the mechanism
  the detection rides on is.
- §10 in its entirety (cloud→desktop relay, proxy agent type). Do not build toward it.

---

## 2. Settled decisions

### D1 — The contract is a declared subset of the one kit tree, not a copy

`docs/local_agent_kit/` stays the single source. There is **no second tree** and no
`contract/` subdirectory in the repo. The contract is a *member list* the kit service
knows about, rendered from the same snapshot and packaged into a second tarball.

Contract members: `kit.json`, `layout.json` (new), `CONTRACT_VERSION` (new),
`CHANGELOG.md`, `schema/**`, `templates/**`.

Why: two trees means two truths, and the drift would be invisible until a desktop
scaffold and a `kit.py new` scaffold stopped matching — which is the exact failure §9.1
exists to catch.

### D2 — Answer to the handover's **Q1**: the contract lives merged at `.cinna-kit/` root

A full kit install already puts `kit.json`, `schema/` and `templates/` at `.cinna-kit/`
root; `layout.json` joins them there. So **`<workshop>/.cinna-kit/` is a valid contract
tree** and the desktop's `contractStore` detector (`kit.json` + `layout.json` at root)
finds it with no change. No `contract/` subpath will ever exist.

The `VERSION` filename collision the desktop flagged is resolved by not overloading it:

- `.cinna-kit/VERSION` keeps its current meaning — the **kit** content version — in both
  the full kit and (absent) the contract tarball.
- The **contract** version is `kit.json` → `contract_version`, and additionally a literal
  `CONTRACT_VERSION` file at the same root.
- **The contract tarball ships no `VERSION` file at all.** The desktop must change its
  fallback from `VERSION` to `CONTRACT_VERSION`; `kit.json` remains the primary read and
  is always present, so the fallback should never fire.

### D3 — Answer to **Q2**: the contract freezes exactly two keys of `app-data/desktop.json`

The desktop's preference is right. `layout.json` declares:

```json
"desktop_owned": [
  {
    "path": "app-data/desktop.json",
    "owner": "cinna-desktop",
    "contract_keys": {
      "api_base_url": "Base URL of the desktop's loopback API, e.g. http://127.0.0.1:53411. Rewritten on every start; the port is random.",
      "agent_token": "Bearer token scoped to this one agent. Absent or empty when the agent's Connected toggle is off.",
      "chat_path": "Optional. Path appended to api_base_url for the chat call. Defaults to /chat."
    },
    "notes": "Everything else in this file is the desktop's to shape and change. A tool reads these keys and no others; it never writes the file, never commits it, never prints its contents."
  }
]
```

`chat_path` is optional and defaulted so the wire path is not frozen before the desktop
has built the API. Either key missing, empty, or the file absent ⇒ "not connected".

### D4 — Answer to **Q3(a)**: yes, and it must be proven, not asserted

`POST /api/v1/cli/account/agents` (`account_create_agent`, `routes/cli.py:859`) and the
other account-CLI endpoints are server-side; the `Cloud/<host>/` workspace directory is a
**cinna-cli-local** concept with no server counterpart. The implementer must confirm this
by reading the account-CLI create/update path end to end and record the finding in the
plan's answer-back section — an assertion from the route list is not a confirmation.
**Q3(b)** (`cinna agent import --update` resolving a `publications[]` entry — **in the
agent-root `publications.json`, not in `cinna-agent.json`**, per D11's amendment — by
`platform_url` alone, with `workspace` absent) is a cinna-cli change, §7.

### D5 — cinna-core's template set is authoritative; the desktop re-bundles

Every file in the desktop's authored `templates/` differs from ours, and it is missing
`.claude/settings.local.json`, `.python-version`, `pyproject.toml`,
`workspace_requirements.txt`, and has `files/README.md` where we have `files/.gitkeep`.
Ours ship. The desktop replaces `resources/cinna-kit-contract/` wholesale with the
published contract tarball; byte-identical scaffolds (§9.1) then follow from a shared
input rather than from two teams converging by hand.

Consequence for us: the only template *content* changes we make are the ones §6 asks for
(the desktop paragraph in root `AGENTS.md`, the `Cloud/<host>/` wording, the
`desktop.json` line in the per-agent `AGENTS.md`, and the four new manifest tokens).

### D6 — `cloud_import_excludes` moves to `layout.json` and adopts the desktop's finer list, except `credentials/`

The exclude list leaves `kit.json` `cloud_import.exclude` and becomes
`layout.json` `cloud_import_excludes`. `kit_config()` / `cloud_import_excludes()`
(`kit.py:227`, `:234`) read the new location. [Corrected during planning, verified against
the tree: "one list, one place" undercounts today's state. There are **three** exclude
lists — `kit.json` `cloud_import.exclude`, `DEFAULT_EXCLUDES` (`kit.py:146`), and
`ALWAYS_EXCLUDE` (`kit.py:137`, re-added unconditionally at export, `cmd_export:1370`) —
plus a fourth, non-list filter, `is_env_filename` (`kit.py:127`), applied independently in
`cmd_export`. The consolidation onto `layout.json` is real, but it folds in all four, not
one list.]

Adopt the desktop's authored list (it is a strict improvement: it covers `credentials.json`,
`**/.env`, `**/*.pem|key|p12`, editor dirs, `node_modules/`, `Thumbs.db`) **for everything
except `credentials/`.**

[Corrected during planning, verified against the tree: the `credentials/` behaviour change
below cannot ship, and the verification this decision called for has now been performed
with a negative result. `AgentEnvService.update_credentials`
(`backend/app/env-templates/app_core_base/core/server/agent_env_service.py:267`)
unconditionally overwrites `credentials/README.md` on every credential sync (writes at
`:312-316`), and `PromptGenerator._load_credentials_readme`
(`.../prompt_generator.py:195-221`) reads that file into the agent's system prompt — so a
travelling kit README would inject local-machine `.env` instructions
(`cp credentials/.env.example credentials/.env`, `git check-ignore -v credentials/.env`)
into a *cloud* agent's system prompt until the first credential sync silently replaced it.
`credentials/` is also in `BUNDLE_EXCLUDED_TOPLEVEL`
(`backend/app/services/environments/workspace_classification.py:57-68`), so the two files
would not have survived into a published bundle anyway. **Settled outcome: `credentials/`
stays excluded wholesale.**]

| Today | After | Effect |
|---|---|---|
| `credentials/` — whole directory excluded | `credentials/` — whole directory excluded (unchanged) | Nothing in `credentials/` travels into the cloud workspace; the finer per-file list adopted above does not apply to this directory. |

[Corrected during planning: separately from the `credentials/` question, moving the list
alone would not have reached the outcome originally stated here either way —
`ALWAYS_EXCLUDE` (`kit.py:137`) re-adds `credentials/` unconditionally at export, and
`is_env_filename` (`kit.py:127`) drops `.env.example` independently of any list, because
`".env.example".startswith(".env.")` is `True`. Both would need to change for a
finer-grained `credentials/` policy to ever take effect. Also newly found: the desktop's
authored list carries `**/.env` and `**/.env.local` but not `**/*.env`, so `prod.env` /
`staging.env` at any depth would travel under its semantics — our list gains `**/*.env`.]

See D15: that `**/*.env` finding is superseded there, where the whole dotenv secret rule
moves out of enumerated patterns and into `layout.json` as declared data.

Also new: `README.md` and `Makefile` are excluded at the agent root (the platform provides
its own), anchored so `docs/README.md` and `scripts/README.md` still travel. Pattern
semantics are exactly as `layout.json`'s `cloud_import_excludes_notes` describes; `kit.py`
`is_excluded()` (`:1304`) must be reworked to implement them (anchored-unless-`**`,
trailing-`/` = directory subtree, `*`/`?` within a segment, `**` across segments,
normalise backslashes and leading `./`).

### D7 — `content_hash`: implement §9.3 exactly, and remove the sort ambiguity

Implement the algorithm verbatim. One correction to the handover: it notes the desktop
sorts in UTF-16 code-unit order and that this "can differ only for non-BMP characters".
Do not leave that as a latent mismatch — sort with
`key=lambda p: p.encode("utf-16-be")`, which is byte-for-byte the desktop's ordering, and
say so in a comment naming why. Cost: one line. Benefit: removes a failure whose only
symptom is "unpublished changes forever".

Everything else as written: symlinks never followed and never listed; excluded
directories never descended into; unreadable file hashed as the literal `unreadable`;
line format `<relpath>\0<hexdigest>\n`; result `sha256:<hex>`.

`kit.py export` grows `--hash` (print the hash of the exported tree) and prints the hash
in its summary.

### D8 — Default workshop path becomes `~/Documents/CinnaAgents`

User decision. Assumption A1 confirmed: the guides do default to `~/Documents/MyAgents`,
in `START.md:20,24,28,73`, `assistants/codex.md:18`, `guides/11-go-cloud.md:66`,
`guides/05-schedules.md:111`. Change all of them. [Corrected during planning, verified
against the tree: the path is not confined to the kit. Five more references exist outside
it, one of them a user-facing frontend surface: `backend/app/services/cli/local_agent_kit_service.py:7`
(module docstring), `frontend/src/components/Onboarding/GettingStartedModal.tsx:224` (the
"What it creates" folder tree), `.cinna-core-kit/scripts/check_docs_references.py:285`
(comment), `docs/application/local_agent_kit/local_agent_kit.md:83,205`,
`docs/application/local_agent_kit/local_agent_kit_tech.md:338` — all five must change too.
`docs/drafts/local-agent-kit_plan.md` is a historical artefact and is deliberately left <!-- nocheck -->
alone.]

### D9 — Contract tarball layout and version endpoint shape

- `GET /agent-start/contract.tar.gz` (+ the `/api/agent-start` alias, same router). The
  archive has **one top-level directory, `cinna-contract/`**, mirroring how
  `kit.tar.gz` roots at `cinna-kit/`. The desktop must descend one level, exactly as
  `kit.py` `_locate_extracted_kit` (`:1212`) already does. Flag this to the desktop team:
  their handover says "extracts to a tree whose root holds `kit.json` and `layout.json`",
  which is true of the extracted directory but not of the archive's top level.
- `GET /agent-start/contract/version` returns
  `{"contract_version": "1.0.0", "kit_version": "<content version>", "platform_url": …,
  "instance_name": …}` — the same envelope as `/version` plus the new key, so
  `_parse_remote_version` (`:1196`) generalises to take the key name rather than being
  duplicated.
- Both inherit the existing surface's behaviour without exception: unauthenticated, the
  `local_agent_kit_enabled` 404 guard, the rate limiter, per-representation ETags,
  `X-Kit-Version`, `Cache-Control`. Do not fork the response plumbing.
- `contract_version` starts at **`1.0.0`**, matching the desktop's pin, in a hand-maintained
  literal `docs/local_agent_kit/CONTRACT_VERSION` (not a `{{TOKEN}}` — unlike `VERSION`).

### D10 — `kit.py chat` wire contract

`kit.py chat Local/<slug> "<prompt>"`:

1. Read `<agent>/app-data/desktop.json`. Missing file, missing/empty `api_base_url` or
   `agent_token` ⇒ exit non-zero with one line saying the desktop is not connected.
2. `POST {api_base_url}{chat_path or "/chat"}`, `Authorization: Bearer <agent_token>`,
   body `{"prompt": "<text>"}`.
3. Response is newline-delimited JSON. Print the text of each line as it arrives; be
   tolerant about the key (`text`, then `content`, then `delta`), because the desktop has
   not built this API yet. A line with `"type": "error"` prints to stderr and sets a
   non-zero exit. End of stream with no error ⇒ exit 0.
4. Connection refused, 401, or any non-2xx ⇒ exit non-zero with a one-line explanation.
   **Never** fall back to role-play.
5. Standard library only (`urllib.request`), like the rest of `kit.py`.

The tolerant key handling and the `chat_path` default are the seam that lets the desktop
build its API without a second round-trip to us. Both are documented in the answer-back
section so they do not become accidental permanence.

### D11 — Manifest schema: adopt the desktop's authored schema

The two schemas are otherwise **identical** — the desktop derived theirs from ours. The
only deltas are the five new properties (`id`, `contract_version`, `runtime`,
`publications`, `created_at`), the changed descriptions on `cloud` / `kit_version` /
`schema_version`, `required` dropping to `["name","slug","description"]`, and the
top-level legacy-exemption `allOf`. Take their file as the starting point, keep our `$id`,
and diff the result against ours to confirm nothing else moved.

Everything §3 of the handover specifies holds, including: `runtime.credential` is a
reference and never a value; `cloud` retained-but-deprecated and migrated into
`publications[]` on the next write; `schema_version` tolerated, non-`const`, non-required,
and **nothing branches on its value**.

[Amended during planning, ruled by the team lead: **`publications[]` leaves the manifest.**
It becomes a sibling file, `publications.json`, at the agent root, named in `layout.json`
`cloud_import_excludes` as the plain string `publications.json`. D11 is amended, not
superseded — everything above stands, with one substitution: "`cloud`
retained-but-deprecated and migrated into `publications[]` on the next write" now means
migrated into `publications.json`, and the manifest schema drops the `publications`
property it currently carries (`schema/cinna-agent.schema.json:218-252`).

**The defect that forced it.** Both hosts hash `cinna-agent.json` — neither exclude list
contains it. So `publications[].content_hash` is a value stored *inside the file it is a
hash of*. Reaching that fixed point requires producing a SHA-256 preimage on demand: it is
not difficult, it is impossible. Concretely: the first publish computes h0, writes it into
the manifest, the manifest bytes change, the next scan computes h1 ≠ h0, and the folder
reads "1 unpublished change" the instant the publish *succeeds*. Republishing to clear it
creates the next mismatch; publishing to instance B makes instance A read "behind". This is
D7's "unpublished changes forever" reached by a third route, and it is unfalsifiable from
the user's side — they see a number that never reaches zero and nothing explains it. Nobody
would have decided this; it is simply what you get by writing the field into the file you
already hash.

**Why the alternative was rejected.** The considered alternative (R2) was to keep
`publications[]` in the manifest and declare a strip-rule in `layout.json`, so the hash is
computed over a canonicalised copy with those keys removed. It is coherent, and would have
been taken if its fail-safe held. It does not. The desktop's `parseLayout`
(`src/main/kit/layout.ts:183-231`) builds its result field-by-field from a known key list
and **silently ignores any top-level block it has never heard of**, and their version gate
passes any same-major pair — pinned by their own test, `checkContractCompatibility('1.0.0',
'1.4.2')` is `ok` (`src/main/kit/contractVersion.test.ts:52`). So a strip rule shipped at
1.1.0 reaches an existing desktop as *silence*: rule ignored, no warning, hash computed the
old way, folder reported healthy, number never zero. That is the D15 fuse one level up, and
the only channel that reaches a non-adopting reader loudly is a **major** bump — on a
contract being shipped at 1.0.0. R2 also contracts a permanent cross-language
canonical-serialisation obligation: Python and JavaScript already disagree on `1e-07` vs
`1e-7` and on integers past 2^53. R4 dissolves the problem instead of paying that liability
to manage it.

**Why this is a correction, not a compromise.** The objection is that splitting
`publications[]` out breaks `layout.json`'s own framing of the manifest as "the one file
every tool agrees on" (`layout.json`, agent role `manifest`). It does not. `publications[]`
was never identity — `id`, `name`, `slug` and `description` are. `publications[]` is a
**mutable per-instance sync ledger** that changes every time an unrelated host pushes, and
it never belonged in a file whose stated job is definitional metadata that travels
unchanged. Storing a hash of a file inside that file was not a bug that happened to land in
the manifest; it was the **symptom that exposed the category error**. R4 puts the ledger
where a ledger goes, and the "one file every tool agrees on" claim becomes *more* true, not
less.

**Why now.** Nothing writes `publications[]` today, which is what makes this cheap — not
what makes it postponable. If `publications[]` ships inside the manifest at 1.0.0, moving it
later costs a major bump, and so does R2's fail-safe. Both roads cost a major bump if taken
later; only this one costs nothing if taken now. This is the cheapest the decision will ever
be.

Four implementation constraints, each one a place the implementation could silently go
wrong:

1. **Export must stop rewriting the manifest entirely.** That is R4's structural dividend
   and it is not to be half-taken. `publications.json` is excluded, so there is nothing to
   clear. Do **not** reintroduce a rewrite to strip a legacy `cloud` key from the exported
   copy — that recreates the exact defect class just fixed (an export that mutates the
   manifest) and breaks byte-parity with a host that uploads verbatim.
2. **Legacy `cloud` migrates at write time, never at export time.** A tool that writes the
   manifest for any other reason migrates `cloud` into `publications.json` and drops the
   key. That moves the folder's hash once — correct, because the folder genuinely changed,
   and visible and explainable rather than perpetual. A legacy folder exported without ever
   being re-stamped carries a stale `cloud` block to the cloud: accepted, there is no secret
   in it, and the remedy is the re-stamp already being asked for.

   [Amended again during planning, verified against the tree, ruled by the team lead: the
   last clause above is false and is withdrawn. **There is no re-stamp a user can perform
   today.** `write_manifest` (`kit.py:426`) is the sole writer of `cinna-agent.json`
   (`:512`), and it has exactly one caller — `cmd_new` (`:1638`), the scaffold path.
   `kit.py`'s five verbs are `new`, `validate`, `list`, `refresh` and `export`; not one of
   them rewrites an existing manifest. The escape hatch named an action nobody can take.

   **What replaces that clause:** migration happens at write time; the only write path
   today is the scaffold; the first real caller will be the publish path or a re-stamp
   verb, **neither of which exists on any host yet**. Until one does, a legacy folder
   keeps its `cloud` block and exports it untouched — harmless, because it carries no
   secret and the platform ignores it.

   **Ruled: soften the wording, do not add a `migrate` verb.** The kit is unreleased,
   no host has a publish path, and nothing writes `publications[]` anywhere — so the
   population of folders needing a re-stamp today is approximately our own development
   tree, and inventing user-facing surface for a user who does not exist is the tail
   wagging the dog. The wording still changes, because an escape hatch that names a
   fictional action is worse than one that admits the gap: the first stops anyone looking
   for the gap.

   **The migration code stays, and stays correct.** Its defects are being fixed now, not
   when a caller appears — and the reason is counter-intuitive: *precisely because the path
   is dead.* When the publish path arrives and becomes its first real caller, nobody will
   remember the function was never exercised, and its defects are the silent-data-loss kind
   that surface once, in a user's folder, irreversibly. **Dead code that will certainly be
   woken is worse than live code, because it carries the appearance of having been used.**]

3. **Root-anchored only** in the exclude list: `publications.json`, **not**
   `**/publications.json`. The paired root-anchored-plus-`**/` rule recorded in D6/D14 and
   in `layout.json`'s `cloud_import_excludes_notes` applies to *directory* patterns; a
   nested `publications.json` under `files/` is a user's file, not ours.
4. **The fail-safe lesson is adopted regardless**, even though R4 removes the rule it was
   about: for anything that can affect a hash, **unevaluable ⇒ refuse to emit `content_hash`
   at all**, never emit one computed a different way. A missing drift number is visible and
   recoverable; a plausible wrong one is neither. This is the second half of a pair whose
   first half is `secret_files`' opposite-direction fuse — stated in full in D15, and it is
   the pair, not either half, that is the lesson.]

### D12 — §8.2 `POST /api/v1/cli/account/desktop-token`

Exchange a CLI **account** token for desktop tokens. Design constraints, all of which the
implementer must satisfy against the existing services rather than inventing a parallel path:

- Authenticated by the CLI account token in the usual account-CLI way
  (`AccountCLIContextDep`, `routes/cli.py`). The CLI token is **used, never stored**, and
  is neither consumed nor revoked by the exchange.
- Body carries the desktop client id (and, for an unregistered client, the lazy-registration
  fields `desktop_auth.authorize` already accepts: `device_name`, `platform`, `app_version`).
  Resulting tokens are **bound to that client id**.
- Response: desktop access token, refresh token, and the account owner's email address.
- The issued client and refresh token must appear in `GET /desktop-auth/clients` as a
  distinguishable entry the user can revoke, with an origin that says it came from a CLI
  token exchange rather than a browser consent.
- It must reuse `DesktopAuthService`'s issuance and rotation, so the desktop's existing
  refresh, reuse-grace and revocation paths (see `docs/` on desktop auth refresh) apply
  unchanged. A token minted by a bypass path that the rotation logic has never seen is the
  failure mode to avoid.
- Security review is mandatory on this endpoint: it converts one credential class into
  another, so the audit trail (a security event naming the exchange) and the scope of what
  the resulting token can do are the two things to get right.

### D13 — `credentials[].type` becomes advisory in the schema, not a closed `enum`

Phase 2's adoption of the desktop's manifest schema (D11) surfaced a contract
inconsistency D1–D12 did not cover: `credentials[].type` is a closed 12-value `enum` — a
hard error — in the shipped schema, but an unrecognised type is a **warning** in the
desktop's validator (`src/main/kit/validator.ts:353-359`, `manifest.credentials.type_unknown`,
"It will be sent to the platform as-is"). Brief §4 already settles our validator to warning
too, which aligns the two validators with each other while leaving both out of step with
the schema they both ship.

**Ruling: drop `enum`, keep `"type": "string"`, add `examples` carrying the twelve current
values, and a description stating: these are the platform credential types known at
contract 1.0.0; the platform's list grows independently of any bundled contract; an
unrecognised value is tolerated and reported as a warning.** Both validators then agree
with each other and with the schema.

This is the same shape as D6: a closed enum inside a contract that is pinned, bundled and
carried offline is a time bomb with a date on it — the first credential type the platform
adds retroactively invalidates every folder that uses it, on a desktop that cannot be
updated to learn about it. The desktop team reached warning-level from exactly this
consideration and were right. The typo argument for keeping it closed does not survive
contact: a warning catches a typo just as visibly, and a genuinely wrong type fails at
import, where the authoritative list actually lives.

Accepted cost, stated plainly: **the schema no longer mechanically rejects an unknown
type.** That is a real loss, accepted on the grounds that a contract's job is to describe
a folder shape that travels, not to mirror a server-side enum it cannot track.

Two follow-ons:

1. The answer-back must say **we** changed our schema to match their validator, not that we
   are asking them to change their validator to match our schema. They raised this as a
   severity choice and should know it propagated into the contract itself.
2. This is now a genuine divergence from the desktop's **authored schema file**, which
   still carries the closed enum — so it joins the required desktop-side changes list,
   making it **seven**: the four in §8, `credentials/` in the exclude list (D6), the
   `desktop_owned` object-form parser fix, and this.

### D14 — `.mypy_cache/`, `.ruff_cache/` and `temp/` join the shared exclude list

Phase 4 replaced the old exclude machinery with the contract list from `layout.json`.
Three directories that used to be dropped no longer are: `.mypy_cache/` and `.ruff_cache/`
were removed silently by `iter_files`'s `SKIP_DIRS`, and `temp/` was in the old
`DEFAULT_EXCLUDES`. The contract list names none of them, so all three now travel to the
cloud. The implementer's first reading was that this is *parity* — the desktop uploads
them too — rather than a regression.

**Ruling: add all three to the shared list.** Parity is the *mechanism* being bought here,
not the goal. "Both sides upload a `.mypy_cache`" describes an agreement to do something
bad in unison. `temp/` is the worst of the three — it was already excluded, so dropping it
is a straight regression introduced under cover of alignment, and it is the one most
likely to hold something the user did not mean to publish.

`kit.py`'s `DEFAULT_EXCLUDES` must stay content-identical to `layout.json`'s
`cloud_import_excludes` — an invariant the implementer verifies mechanically. This is also
a required desktop-side change.

### D15 — the dotenv secret rule becomes declared data in `layout.json`, not code in `kit.py`

The plan told the implementer to delete `kit.py`'s `is_env_filename` short-circuit and rely
on the exclude patterns alone. They measured that premise and found it **false**: the
contract list *enumerates* dotenv suffixes (`**/.env`, `**/.env.local`, `**/*.env`), so
`.env.production`, `.env.staging` and `.env.development` travel at any depth on both
sides. They kept a narrowed guard instead, which also fixed the separate defect where
`.env.example` was being eaten (`".env.example".startswith(".env.")` is `True`).

**Ruling: keep the fix, but express the rule as data.** A hand-rolled secret filter living
in `kit.py` re-creates precisely what D6 and `layout.json` exist to end — a folder rule
each host re-implements from prose. Two such implementations will diverge, and the
divergence is invisible until it isn't. Enumerated suffixes mean `.env.prod` leaks the day
someone types it, and that is a secret leaving the machine, which is the worst outcome
this feature can produce.

So the rule goes into `layout.json` as a declared block, in the spirit of the existing
`local_command_runner` block — that precedent is why this is a natural shape rather than a
special case. Semantics: a file is secret and never travels when its basename is `.env`,
starts with `.env.`, or ends with `.env` — **unless** the basename ends with one of a
declared allowed-non-secret suffix list (`.example`, `.sample`, `.template`). Exact field
naming and structure are the implementer's call; the required properties are that it is
data, that it closes both the `prod.env` and `.env.production` cases both sides currently
leak, and that `.env.example` still travels. `kit.py` reads the rule rather than knowing
it.

**The fail-safe direction, and its pair.** The block declares its own fail-safe: a clause a
host cannot evaluate resolves toward treating the path as secret — an unknown `match` clause
counts as a hit, an unknown `unless` clause counts as a miss (`layout.json`,
`secret_files.notes`). The same discipline runs the *opposite* way for `content_hash`, and
D11's amendment adopts it there: **unevaluable ⇒ refuse to emit `content_hash` at all**,
never emit one computed a different way. For secrets, unevaluable ⇒ withhold the file; for
hashes, unevaluable ⇒ withhold the number. The two directions are opposite because the
consequences are: a leaked secret is unrecoverable, while a plausible wrong drift number is
untraceable and a missing one is merely visible. So **the safe direction is a property of
the consequence, not a house style** — that is the transferable part, and a host that copies
the direction instead of deriving it will get the next case backwards.

**D15 subsumes the earlier `**/*.env` finding** recorded in D6's correction notes (plan §1
correction 5) — that finding is not tracked separately from here on.

### D16 — the contract tarball asserts its own completeness and version agreement

Phase 3 built `/agent-start/contract.tar.gz` and `/agent-start/contract/version`, and found
the two representations degrade **asymmetrically**. `/contract/version` fails loud (503
when `CONTRACT_VERSION` is absent, empty or undecodable). `/contract.tar.gz` degrades
**silently** — a 200 with a truncated archive and a perfectly valid ETag — because the
member selector is a *filter*, not an assertion. This is not hypothetical: on the current
instance, whose synced snapshot predates Phase 1, `/contract/version` returns 503 while
`/contract.tar.gz` returns 200 with a 33-member archive missing both `layout.json` and
`CONTRACT_VERSION`. A desktop pulling that gets a tree its own `contractStore.isContractTree`
will not recognise, with nothing anywhere explaining why.

**Ruling: guard inside `get_versioned_contract_tarball()` only** — not in
`_build_or_cached`, and not at the route. Putting it in the shared build path would 503 the
*entire* anonymous surface (`START.md`, `/version`, `kit.tar.gz`) whenever a snapshot lacks
the contract files, breaking a surface whose whole design premise is that it degrades
gracefully for an anonymous caller. Scope the failure to the representation that is
actually broken.

**Load-bearing member set: `kit.json` + `layout.json` + `CONTRACT_VERSION`.** The first two
are the desktop's own identity pair, so an archive missing either is not a contract tree
by their definition and there is nothing to be gained by shipping it; the third is what D2
makes the version fallback rest on. `schema/` and `templates/` being present-but-thin is a
**content** problem, and a content problem must not be dressed up as an identity failure.

**Fold in the version-agreement check.** Three `1.0.0`s ship inside one archive —
`CONTRACT_VERSION`, `kit.json.contract_version`, `layout.json.contract_version` — with
nothing in the serving path noticing drift, and a Phase 12 invariant test only catches it
if someone runs Phase 12's tests before shipping. The packer already has all three files
open: have it verify the three agree and 503 on disagreement, same as an absent member.
Cheap, and it converts a silent inconsistency into a loud one at the only moment anyone can
act on it.

**This is the third instance of one shape in this work, and the answer-back must name it as
a pattern rather than as three unrelated findings.** A *filter used where an assertion
belongs*, degrading to a plausible-looking success:

1. `asStringArray` silently yielding `[]` for the `desktop_owned` object form
   (`layout.ts:228`) — see D3 and the plan's §0.
2. The stale-snapshot test green: `docs/` is not mounted into the backend container, so a
   container test run reports success while exercising none of the working tree.
3. This one: `_is_contract_member` filtering rather than asserting, so an incomplete
   contract ships as a 200.

All three are silent, all three produce a result that looks like success, and in all three
the cost lands on someone downstream with no way to trace it back. This belongs in the
answer-back as its own paragraph, because the desktop team is about to write a great deal
of code against this contract and it is the failure mode they will meet most often.

[Amended during planning, verified against the tree: the guard as scoped above is
incomplete. With it applied only to `get_versioned_contract_tarball()`, `/contract/version`
still publishes an unvalidated number, because `_contract_version_from` asserts only
"non-empty and decodable" — not that the number it publishes is the one the contract
actually carries:

| snapshot defect | `/contract.tar.gz` | `/contract/version` |
|---|---|---|
| `layout.json` missing / unparseable | 503 | **200, `"1.0.0"`** |
| `kit.json` missing / no `contract_version` | 503 | **200, `"1.0.0"`** |
| `CONTRACT_VERSION=2.0.0`, others `1.0.0` | 503 | **200, `"2.0.0"`** |
| `CONTRACT_VERSION` missing / empty | 503 | 503 |
| `schema/`+`templates/` thinned | 200 | 200 |

**Amendment: both contract representations degrade together** —
`_assert_contract_coherent` applies to `/contract/version` too, not to
`/contract.tar.gz` alone.

The boundary above still holds and does not need re-litigating: the narrow scoping exists
to protect the **anonymous kit surface** — `START.md`, `/version`, `kit.tar.gz`,
`/kit/{path}` — which must keep serving to a stranger regardless of contract health.
`/contract/version` is **not** part of that surface; it is the contract's second
representation, and two representations of the same thing reporting different health is
the same asymmetry this decision was written to remove, one endpoint over. Verified: with
`layout.json` deleted, all four kit-surface paths still answer 200 while both contract
representations 503. The thin-`schema/` negative control still 200s on both, since a
content defect must not present as an identity failure.

The third table row is what settles it: an endpoint the desktop *polls*, publishing an
unvalidated number that §4's major-version compatibility gate keys on, is worse than
useless. The stale-`CONTRACT_VERSION` direction is worse still: a false *compatibility*
fails silently, where a false incompatibility at least stops.]

### D17 — `schema_version` leaves the `/agent-start/version` response shape, and `kit.json` with it

**The change a reader must take away first is a change to an endpoint, not to a file.**
`GET /agent-start/version` — unauthenticated, served by every instance, and the endpoint
`kit.py refresh` polls — returned a `schema_version` key on every response. It no longer
does. `docs/local_agent_kit/kit.json` carried the same field and loses it in the same
change. Both are true, but they are two different facts to a consuming host: one is an
edit to a file they bundle and can read for themselves, the other is a change to a wire
shape they may one day poll — and **the second is the one that could surprise someone**.

[Framing corrected after implementation, and recorded because the correction changed the
ruling rather than decorating it. D17 was ruled from the belief that the backend site was a
*fallback* literal — a synthesised default standing in for `kit.json` when the file could
not be read. It is not. `_version_payload`
(`backend/app/services/cli/local_agent_kit_service.py`) is the sole builder of the live
`/version` envelope, so the literal sat on the wire of a public endpoint, not in a degraded
path nobody reaches.]

**The corrected characterisation strengthens D17; it does not weaken it.** A number that
decides nothing, sitting on the endpoint `kit.py refresh` actually polls, is a sharper
instance of "the next tool treats it as meaningful" than the same number on a path nobody
hits: a dead field in a bundled file can only mislead someone who reads that file, while a
dead field on a polled response is offered, unprompted, to every client that ever parses
it. **The corrected framing is a better justification for the decision than the one it was
originally ruled from** — which is the reason to record the correction rather than quietly
absorb it.

**Nothing reads it — verified, not assumed.** `kit_config()` (`kit.py`) has exactly two
call sites: `contract_version()`, reading `contract_version`, and `cmd_refresh`, reading
`kit_base_url`. `_parse_remote_version` accepts only `kit_version` / `version` (and
`contract_version` for the contract endpoint), so `refresh` never looks at the field even
though it polls the very response that carried it. The desktop's two `kit.json` readers —
`contractStore.ts` `readVersionAt` and `agentsHomeService.ts`
`readInstalledContractVersion` — each read `contract_version` and nothing else, and the
desktop does not call `/agent-start` at all.

**Why "no known reader" is not the argument that carries this, and what is.** The endpoint
is unauthenticated and served by every instance, so an exhaustive search of our two
repositories does not establish that no consumer exists — only that none we can see does.
On a public wire, absence of evidence is genuinely weak evidence of absence, and a decision
resting on it would be resting on the reach of a grep. What makes the removal safe
regardless is the **nature of the field: it was a synthesised constant.** The literal `1`
was compiled into the payload builder. It never varied across instances, versions or
requests, and no input could have made it vary. A hypothetical external consumer reading it
therefore learns nothing from it, and nothing downstream can be computing anything from a
value that has only ever had one value.

Stated generally, because the general form is the reusable half and will apply again the
next time a field is retired from a public surface: **a constant field is exactly the one
whose removal breaks only code that reads it without using it.** Anything genuinely
branching on it would have to branch on a value it always sees, which is not a branch. That
property — not the empty grep — is what carries the risk here, and it is the question to
ask of the *next* field, which may not have it.

The reasoning is the reasoning of the whole feature: this work exists to eliminate the
second number that decides nothing. That is D11's argument — `schema_version` tolerated,
non-`const`, non-required, and **nothing branches on its value** — and it is D13's argument
one level up, that a pinned, bundled, offline-carried contract must not freeze a value it
cannot track. It is also why `contract_version` was introduced at all. `kit.json` is half
the identity pair the desktop reads a contract tree by (D2, D16), so leaving the field
*there* ships, in the most load-bearing file of the contract, the exact artefact this run
removed from everywhere else.

**"Inert" is a description of today, not a property.** Nothing anywhere prevents the next
tool — ours, the desktop's, or a third host reading the contract cold — from finding an
integer named `schema_version` sitting beside `contract_version` in the contract's index
file and concluding it means something. A gate that exists but is never consulted is
indistinguishable, to a reader meeting it for the first time, from a gate that is. This is
the same shape as D11's dead-migration-code argument, run the other way: *dead code that
will certainly be woken is worse than live code, because it carries the appearance of
having been used* — and a dead **field** in a published contract carries that appearance to
every implementer who will ever read it, none of whom were here for the decision that made
it inert.

Two facts make this cheap as well as principled, and both belong in the decision rather
than in the round that implements it:

1. **It moves toward the desktop's file, not away from it.** Their authored
   `resources/cinna-kit-contract/kit.json` carries no `schema_version` at all — its
   top-level keys are `name`, `title`, `description`, `contract_version`, `schema`,
   `layout`, `templates`, `refresh`. Ours was the only one of the pair carrying the field,
   so removing it is convergence, and unlike D13 it costs the desktop no change at all.
2. **The kit is unreleased.** This is R4's timing argument from D11, applied unchanged:
   free now, a major bump later. Nothing reads the field, no host has shipped against it,
   and the population of folders affected is our own development tree. If it ships at
   1.0.0 it can only leave at 2.0.0.

**The two sites move together, and that is a constraint on the change rather than a
convenience.** **A payload that disagrees with the shipped file is worse than either state
alone** — a number served on the wire with no backing artefact in the tree reads, to anyone
comparing the two, as a serving bug rather than as a deliberate removal, and sends them
hunting a fault that is not there. So they go in one change, and a future edit that
restores either one alone reintroduces the disagreement rather than the field.
`_version_payload` now carries a note saying why there is no `schema_version` and naming the
manifest's field as the different thing it is, so it is not restored later "for parity with
`kit.json`".

**The distinction this decision must not blur, stated because confusing the two would be a
serious regression.** The **manifest's** `schema_version` — in `cinna-agent.json`, in
`schema/cinna-agent.schema.json`'s legacy-exemption `allOf` and its `$comment`, and in
`kit.py`'s `_validate_identity` — is a **real, live, load-bearing legacy marker** and it
**stays**. It is the sole selector for the one tolerated identity absence (`schema_version`
present with neither `contract_version` nor `id` ⇒ pre-1.0.0 folder, warned and re-stamped,
never rejected), it is pinned by the desktop's `validator.ts` and its tests, and the CHANGELOG's
Compatibility table depends on it. D17 removes **only** `kit.json`'s field. The two share a
name and nothing else: `kit.py`'s `MANIFEST_NAME` is `cinna-agent.json`, its `KIT_JSON_PATH`
is `kit.json`, and no code path reads one for the other.

**Test follow-on, for Phase 12 — this is a required change, not an observation.**
`test_version_payload_matches_the_index`
(`backend/tests/api/cli/test_local_agent_kit.py`) asserts
`payload["schema_version"] == index["schema_version"]`. That assertion pins exactly the
coupling D17 dissolves and must be dropped; the surrounding assertions (`kit_version`,
`kit_base_url`, `cli`) still hold and are what the test is for. Unrelated, and **not**
caused by D17: `test_new_scaffolds_and_validate_exits_zero`
(`backend/tests/unit/test_local_kit_tool.py`) fails on `manifest["schema_version"] == 1`,
which reads the scaffolded **manifest** — a pre-existing Phase 7 consequence of the agent
template dropping the key, on the legacy side D17 does not touch.

**The other declarations of this response shape — assigned to Phase 13, as one concern.**
`docs/application/local_agent_kit/local_agent_kit_tech.md`'s endpoint table lists
`schema_version` in the `/version` response, and `frontend/src/hooks/useLocalAgentKit.ts`
declares it on `LocalAgentKitVersion`. Neither is a reader — the hook consults only
`kit_version`, and its cast is unchecked, so nothing breaks and no typecheck fails — but
both now describe a field that does not arrive. They are one job and not two: making every
declaration of this response shape agree with what it actually returns. The `CHANGELOG.md`
entry goes to that file's owner, with this same endpoint framing.

---

## 3. Guide and template work (handover §6)

All of it, plus D8's path change. Specifically: `Cloud/<host>/` per-instance workspaces in
guide 11 and the root templates [Corrected during planning, verified against the tree: this
is not wording-only. `cmd_list` (`kit.py:1119`) hard-codes the flat layout —
`root/"Cloud"/".cinna"/"account.json"` and `root/"Cloud"/"agents"` — so `kit.py` itself
changes with the `Cloud/<host>/` move, not just the guides and templates]; the
`desktop.json` paragraph in guide 03 and the per-agent
`AGENTS.md`; the "If this workshop is managed by Cinna Desktop" section and the `chat` row
in the root `AGENTS.md`; a new `assistants/cinna-desktop.md`; guide 10's "test through
`kit.py chat` when the marker exists, keep role-play as the fallback"; guide 01 §2 gaining
`--description`; guide 12's `schema_version`-is-breaking rule becoming
`contract_version`-major-is-breaking, plus the contract's own changelog and version endpoint.

`docs/local_agent_kit/CHANGELOG.md` gets the Compatibility table from handover §4 verbatim,
because three implementations now key off it.

---

## 4. Validator parity (handover §5, §9.2)

`kit.py validate` gates on `contract_version` per §4 and accepts the new fields. The
kit-only findings — the ones the desktop cannot implement and which must therefore be
**warnings, never errors** — are to be fixed as an explicit, named list in the plan. The
starting set: `_validate_requirements` (`:831`, pyproject ⇄ `workspace_requirements.txt`
reconciliation and its `--fix`), and `template_description()` (`:918`, "description is
still the template default"). Any check the implementer finds that the desktop's validator
does not have must be added to that list or demoted, not left to diverge.

Mirror the desktop's two severity choices: an **unknown credential `type` is a warning**,
and a **catalogued command with no Makefile target is a warning**. (The schema itself no
longer enforces a closed `enum` on `credentials[].type` either — see D13.)

`runtime.credential` that looks like a secret (`sk-`, `sk_`, `ghp_`, `gho_`,
`xox[baprs]-`, `AIza`, `AKIA`, or >200 chars) is an **error**:
`manifest.runtime.credential_looks_like_secret`, with a rotate-it message.

---

## 5. Deployment and sync checks (do not skip)

- `sync_platform_knowledge.py` mirrors `docs/local_agent_kit/` into
  `platform-knowledge-env/.../knowledge/local-kit/`. New files (`layout.json`,
  `CONTRACT_VERSION`, `assistants/cinna-desktop.md`) must ride that sync — confirm the
  sync is path-glob based and not an enumerated file list.
- The kit service reads the tree from a **snapshot** shipped in the image
  (`_read_snapshot`). Confirm new files are inside whatever the snapshot copies, or they
  will 404 in a deployed instance while working locally.
- `frontend/nginx.conf` and the Vite dev proxy already match `/agent-start(/|$)`; the two
  new paths sit under it. Verify, do not assume.
- Scaffold ignore files must keep shipping dotless (`gitignore`) — git hides scaffold
  content in both the source tree and the snapshot otherwise.

## 6. Regression scope

`docker compose exec backend python -m pytest tests/api/ -k "local_agent_kit or agent_start"`
plus the desktop-auth and account-CLI groups for §8.2. A `kit.py` unit surface exists —
find it and extend it; the `content_hash` algorithm and `is_excluded` pattern semantics
each want a table-driven test, because both fail silently.

## 7. cinna-cli follow-ups (separate repo, not this work)

Record them, do not implement them:

1. `cinna agent import` writes a `publications[]` entry (migrating `cloud`) with
   `platform_url`, `agent_id`, `workspace`, `imported_at`, `updated_at`, `contract_version`,
   `content_hash`. **The file is `publications.json`, a sibling of `cinna-agent.json` at
   the agent root — never `cinna-agent.json` itself** (D11's amendment). Writing it into
   the manifest recreates the fixed-point defect that amendment exists to remove, and this
   item is an instruction to a separate repository, so the file must be named here rather
   than inferred.
2. `--update` resolves the entry **in that same `publications.json`** by `platform_url`,
   tolerating an absent `workspace` — answering Q3(b).
3. `local_import.py` sources its exclude list from `layout.json`.
4. The import gates on `contract_version` per §4.

## 8. The answer-back document

The deliverable includes a reply to the desktop team, written to
`docs/local_agent_kit/desktop_contract_answers.md` (and copied into the handover's repo is
the user's call, not ours). It must answer Q1/Q2/Q3, confirm or correct each of A1–A8,
state the §8.3/§8.4 scope exclusion and what it costs them, and gather, in one place, an
**unnumbered list** of the things they must change on their side. Seed that list with the
four found while writing this brief — the `CONTRACT_VERSION` fallback (D2), the one-level
descent into `cinna-contract/` (D9), re-bundling our templates (D5), and the `desktop.json`
key names plus the `chat` wire shape (D3, D10) — and add to it whatever the plan's §0
Answer-back findings turn up beyond those four. Do not assert a count anywhere else in the
document; if the answer-back states how many items there are, that number must be
generated from this one list, at the end, not carried as a number decided in advance —
this list has already grown twice since it was first written down.
