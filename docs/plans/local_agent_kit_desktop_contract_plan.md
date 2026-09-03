# Local Agent Kit → Cinna Desktop contract: implementation plan

**Status:** phased implementation plan. Input: `docs/plans/local_agent_kit_desktop_contract_requirements.md`
(decisions D1–D17, settled — this plan executes them, it does not re-open them).
That range is stated as of this writing and has already grown once past what this header
originally claimed — D13 opens by naming an inconsistency "D1–D12 did not cover".
**Re-derive it from that file (`grep -n "^### D[0-9]"`) rather than trusting this line**,
per the standing checklist's rule on figures quoted forward.
Source request: `/Users/evgenyl/dev/ml-llm/cinna-desktop/docs/agents/local_agents/cinna_core_handover.md`.
Section numbers written as "§N" are the handover's unless prefixed "brief §N".

**In scope:** handover §2, §3, §4, §5, §6, §7, §8.1, §9.

**Out of scope, deliberately — no phase below may touch these:**

- §8.2 `POST /api/v1/cli/account/desktop-token` (brief D12). Handled in a separate run.
- §8.3 server-side import changes, §8.4 account-CLI accepting desktop tokens.
- §10 (cloud→desktop relay, proxy agent type).
- The answer-back document `docs/local_agent_kit/desktop_contract_answers.md` (brief §8).
  A separate run writes it, consuming **§0 Answer-back findings** below.
- **Files no phase may modify:** `backend/app/api/routes/desktop_auth.py`,
  `backend/app/services/desktop_auth/`, `backend/app/api/routes/cli.py`.

**Reference tree, READ-ONLY:** `/Users/evgenyl/dev/ml-llm/cinna-desktop/`. Another team's
repo, possibly with their agents active in it. Read it; never write, edit, or run
anything in it.

---

## §0 Answer-back findings

**This is a live section.** Every phase that produces a verified fact appends it here.
The answer-back document is written from this section and from nothing else. A bucket
marked TODO is not answerable yet; a bucket marked VERIFIED carries its evidence inline
and must not be softened into an assertion later.

### Bucket 1 — D4: does the account-CLI create/update path need a local `Cloud/<host>/`?

**Status: VERIFIED (planning run). Answer: split, and the split is the point.**

Functions read end to end (READ ONLY; this plan modifies none of them):

- `backend/app/api/routes/cli.py` — `account_create_agent:859`, `account_connect_agent_api:888`,
  `account_agent_api_enable:923`, `account_agent_api_refresh:952`, `account_agent_api_spec:979`,
  `account_agent_api_call:1002`, `account_restart_env:1037`, `account_inspect_agent:1071`,
  `account_list_discoverable_mcp:1096`, `account_connect_mcp:1114`, schedules
  `1163/1182/1206/1235/1270/1303/1339`, `account_agent_status:1369`,
  `account_set_status_refresh_command:1397`, credentials
  `1444/1457/1479/1503/1530/1569`, `account_upload_file:735`, `mint_child_token:775`,
  `account_api_proxy:1734`, `sync_stream_ws:558`, `get_workspace:380`,
  `_require_developer_account:844`.
- `backend/app/services/cli/account_cli_service.py` — `create_agent:583`,
  `_resolve_owned_workspace_id:628`, `list_user_workspaces:651`, `mint_child_token:352`,
  `exchange_account_setup_token:267`.
- `backend/app/services/cli/cli_service.py` — `run_sync_tunnel:852`, `get_workspace_tarball:406`.
- `backend/app/models/cli/account_convenience.py` — `AccountAgentCreateBody:21-43`.

Findings:

1. **Every DB-object action is purely server-side and needs no directory.**
   `AccountAgentCreateBody` carries `name`, `description`, `user_workspace_id` and nothing
   path-shaped. `create_agent` maps to `AgentCreate` and delegates to `AgentService`.
2. **"workspace" server-side is never a directory.** `user_workspace_id` names a
   `UserWorkspace` **DB row**, resolved by `_resolve_owned_workspace_id:628`. The code
   states the opposite of a server-side directory notion explicitly at `cli.py:668-673`
   and `account_cli_service.py:654-658`: *the active-workspace selection lives client-side
   in `.cinna/account.json`; no server-side "active workspace" state is kept.*
   `Cloud/` appears nowhere in `backend/app/**/*.py` outside the shipped local-kit doc copy.
3. **Correction to the handover's framing: there is no account-CLI "agent update",
   "prompts write", "metadata write", "tree upload", "push" or "manifest stamp"
   endpoint at all.** Verified by full enumeration of `cli.py` (1893 lines).
   - prompts / metadata write = `PUT agents/{id}` through the generic
     `account_api_proxy:1734` — exactly what `guides/11-go-cloud.md:167` prescribes.
   - "push" = Mutagen live sync: `sync_stream_ws:558` → `CLIService.run_sync_tunnel:852`
     (an opaque bidirectional byte pump, `cli_service.py:915-932`) → env-core
     `/sync/exec` (`backend/app/env-templates/app_core_base/core/server/routes.py:2043`),
     which spawns `mutagen-agent` with `cwd=WORKSPACE_ROOT` (`routes.py:2115-2126`).
   - "manifest stamp" is a **client-side** write into the local `cinna-agent.json`
     (`docs/local_agent_kit/tools/kit.py:1388-1394`).
4. **Consequence.** Create, prompts, credential drafts, schedules and status are stateless
   HTTP calls the server accepts from any cwd. The *file tree* half needs **a** local
   directory because Mutagen syncs a directory — but not a `Cloud/<host>/` one specifically.
   `Cloud/<host>/` is a pure cinna-cli/kit convention the server neither receives nor stores.

**Answer to Q3(a): yes — with the caveat in (4), which the desktop must be told, because
their Publish design assumes an HTTP tree-upload endpoint that does not exist.**

### Answer-back: the tree-upload gap (own section, do not bury)

**Instruction to whoever writes the answer-back: this finding gets its own top-level
section in that document. Do not fold it into the Q3(a) answer or any other bucket's
prose — Bucket 1 above is the evidence trail, this is the pointer that says "read it, and
weight it accordingly."** It is the most consequential thing found in the planning run so
far, because it means the desktop's Publish design rests on a route that does not exist —
a bigger obstacle than the §8.4 scope exclusion they were expecting.

1. **What does not exist.** There is no account-CLI agent-update, prompts-write,
   metadata-write, tree-upload, push or manifest-stamp endpoint. This is not an inference —
   it was established by full enumeration of `backend/app/api/routes/cli.py` (1893 lines);
   see Bucket 1, finding 3.
2. **What exists instead, and is usable.** The stateless DB-object endpoints (create agent,
   credential drafts, schedules, status, status-refresh-command) need no local directory at
   all. Prompts and metadata go through the generic `account_api_proxy` (`cli.py:1734`) as
   `PUT agents/{id}` — exactly what `guides/11-go-cloud.md:167` already prescribes. The file
   tree moves over Mutagen live sync (`sync_stream_ws:558` → `CLIService.run_sync_tunnel:852`
   → env-core `/sync/exec`), which does require **a** local directory — just not a
   `Cloud/<host>/`-shaped one specifically. The manifest stamp is a client-side write into
   the local `cinna-agent.json` (`kit.py:1388-1394`).
3. **What that means for their Publish design.** Handover §8.3 speaks of "changes to the
   existing agent-import path"; there is no such path on the server to change. A desktop
   Publish flow would therefore need either a local directory it can run a sync against, or
   a new server-side tree-ingest route that does not exist today.
4. **Explicit boundary.** cinna-core is **not** designing or building that route as part of
   this work. This instruction is only to make the gap impossible to miss in the
   answer-back — nothing more. Whoever writes the answer-back must not present a route shape
   as though it were on offer.

### Answer-back: `desktop_owned` object form breaks their parser (own section, rank second)

**Instruction to whoever writes the answer-back: this gets its own top-level section too,
ranked immediately after the tree-upload gap.** Bucket 5a below is the evidence trail;
this is the pointer. The team lead has ruled we ship the D3 object form of `desktop_owned`
anyway, with the consequence recorded here made explicit to the desktop rather than
discovered by them.

1. **The mechanism, exactly.**
   `/Users/evgenyl/dev/ml-llm/cinna-desktop/src/main/kit/layout.ts:228` reads the block as
   `asStringArray(doc.desktop_owned).map(normalizeRelPath)`, and `asStringArray` is a
   `typeof v === 'string'` filter — so an array of objects filters down to `[]`. No throw,
   no `logger.warn`, no degraded-contract path; `KitLayout.desktop_owned` is typed
   `string[]` and `desktopOwned()` simply returns nothing.
2. **The good news, stated as such.** Their own `src/main/kit/layout.test.ts:23` asserts
   `['app-data/desktop.json']` and therefore **fails the moment they pull the real contract
   file** — so their existing suite catches this rather than letting it rot.
3. **Blast radius today is small.** Nothing consumes `desktopOwned()` behaviourally yet —
   only a comment at `src/main/kit/desktopStateService.ts:3` — so this is the cheapest
   moment the fix will ever be.
4. **Why we chose this shape.** The alternative considered and **rejected** by the team
   lead was shipping their string array alongside a parallel `desktop_owned_files` object
   key. That is two lists of the same paths drifting apart forever, to spare an unreleased
   reader a one-line change. We deliberately chose the shape that breaks them **loudly and
   now** over the one that would have degraded **quietly and later**. The string form
   cannot carry `contract_keys`, which is the entire point of D3.

This is one of the required desktop-side changes — see "Required desktop-side changes
(single list, unnumbered)" below for the full list; do not assert an ordinal here. The
answer-back must rank this section second, only behind the tree-upload gap.

### Answer-back: `credentials[].type` enum goes advisory (own section, rank third)

**Instruction to whoever writes the answer-back: this gets a standalone §0 subsection too,
alongside the two ranked ones above. Rank it third** — a real divergence, but a smaller
obstacle than either the tree-upload gap or the `desktop_owned` parser break.

Phase 2 surfaced a contract inconsistency D1–D12 did not cover; the team lead settled it as
**D13**: the schema's `credentials[].type` `enum` is dropped, `"type"` stays `"string"`, and
an `examples` array carries the twelve current values with a description stating these are
the types known at contract 1.0.0, the platform's list grows independently of any bundled
contract, and an unrecognised value is tolerated and reported as a warning. Rationale is the
same shape as D6: a closed enum inside a contract that is pinned, bundled and carried offline
is a time bomb — the first credential type the platform adds retroactively invalidates every
folder using it, on a desktop that cannot be updated to learn about it. Accepted cost,
recorded rather than glossed: the schema no longer mechanically rejects an unknown type.

1. **Their authored schema still carries the closed `enum`**, so D13 is a genuine divergence
   from the file they shipped, not merely a stricter reading of it. It joins the required
   desktop-side changes list — see "Required desktop-side changes (single list, unnumbered)"
   below; do not assert an ordinal here, the list has grown since this section was written
   and will grow again.
2. **Direction of the change, stated explicitly.** We changed **our schema to match their
   validator**, not the reverse. They raised the severity choice themselves
   (`validator.ts:353-359`, `manifest.credentials.type_unknown`, warning) — the answer-back
   must tell them it propagated from their validator into the contract artefact itself.
3. **Cross-reference Bucket 3b/S2**, which already requires `kit.py` to demote an unknown
   credential type to a warning. D13 is what makes that alignment consistent with the schema
   rather than merely consistent between the two validators.

### Answer-back: the `**/`-prefixed-directory-pattern footgun (own section)

**Instruction to whoever writes the answer-back: this is its own answer-back item — do not
fold it into the D14 ask.**

1. **The mechanism.** In the desktop's `matchesPattern` (`src/main/kit/layout.ts`), the
   directory branch only tries path prefixes at least as long as the pattern — so a
   `**/`-prefixed directory pattern can never match at the root. `**/.mypy_cache/` does not
   match `.mypy_cache/x.json`; `**/.git/` does not match `.git/HEAD`. Confirmed against
   their source, not against our port.
2. **Why it matters more than the patterns themselves.** Their authored list pairs almost
   every entry — `.git/` with `**/.git/`, `.gitignore` with `**/.gitignore`, `.gitkeep`,
   `credentials.json` — which suggests they discovered this empirically and never wrote it
   down. **`__pycache__` is the one entry that only ever got the `**/` form**, so a
   root-level `__pycache__/` was travelling under contract semantics (masked for `.pyc`
   files by `**/*.pyc`, but anything else in it went up). Every unpaired directory entry in
   that list is a hole, and nothing tells you. The lead's framing: naming the mechanism is
   worth more to them than the four patterns we are asking them to add.
3. **What we did about it on our side.** We shipped all D14 additions in **paired form**,
   and added `__pycache__/` alongside the pre-existing `**/__pycache__/` — one parallel
   line fixing a pre-existing hole in the same class as D14, explicitly authorised by the
   team lead as verified, minimal and reversible. `cloud_import_excludes_notes` now names
   the pairing requirement and why, so the duplicates are not "simplified" away later. See
   Bucket 9 for the fuller D14/D15 implementation record.

This is one of the required desktop-side changes — see "Required desktop-side changes
(single list, unnumbered)" below; do not assert an ordinal here.

### Required desktop-side changes (single list, unnumbered)

**Do not put a running count anywhere in this document.** The count kept going stale while
sections were written independently — it was asserted as "fifth" (Bucket 2), then "sixth"
(the `desktop_owned` section above), then "that makes seven" (the `credentials[].type`
section above), and every one of those numbers was already wrong by the time the next
section landed: a further item has since been added, and one earlier finding (§1
correction 5) has been folded into another (D15) below. The two ranked sections above have
been edited to point here instead of asserting an ordinal.

- The four originally in requirements-brief §8: the `CONTRACT_VERSION` fallback (D2), the
  one-level descent into `cinna-contract/` (D9), re-bundling our templates (D5), and the
  `desktop.json` key names plus the `chat` wire shape (D3, D10).
  **Sharpened, and the sharp part is the consequence, not the ask.** D5's re-bundle is
  `resources/cinna-kit-contract/` **wholesale — `layout.json` included**, not the
  `templates/` subtree alone. The two bundled `layout.json` files differ **while both
  declare `"contract_version": "1.0.0"`**: ~~theirs carries 34 exclude patterns to our
  40~~ — **both figures corrected in place, re-derived at this point of use by the R4
  review (Bucket 17); the strike-through is kept rather than overwritten, per the standing
  checklist.** Re-derived now by loading both files: **ours is 41** (superseded 40 — see
  Bucket 16h) and **theirs is 32**, not 34; theirs is a strict subset of ours, and the nine
  patterns it lacks are `publications.json`, `temp/`, `credentials/`, `**/*.env`,
  `__pycache__/`, `.mypy_cache/`, `**/.mypy_cache/`, `.ruff_cache/` and `**/.ruff_cache/`.
  **Two riders on the older prose, both of which the re-derivation contradicts:** the "both
  forms of the last three" gloss is wrong for `__pycache__` — they already carry
  `**/__pycache__/` and lack only the root form, which is exactly the unpaired-entry hole
  the `**/`-prefixed-directory-pattern section names; and `publications.json` was not in the
  original list at all, so its absence is a *new* gap opened by R4 rather than one of the
  D14/D15 additions. Theirs still lacks the entire `secret_files` block, and still
  types `desktop_owned` as `string[]` where ours is `object[]`. Same declared version,
  different content — so the version number cannot be used to tell the two apart.
  **Therefore: every hash-parity claim in §0 is a claim about a post-adoption desktop, and
  none of them holds against the build on their disk today.** State that in the answer-back
  in those words; a reader who takes the parity claims as current will conclude the contract
  is already met. See Bucket 15e.
  **Sharpened again, on the D3 half of this bullet rather than as a new entry, because it is
  the same ask with evidence now attached: the desktop does not write D3's frozen keys.**
  `desktopStateService.ts:45,47` declares `localApiBaseUrl` and `agentToken`, and `coerce`
  (`:74`) reads `raw.localApiBaseUrl` / `raw.agentToken` at `:112-113`. The contract's keys
  are `api_base_url` and `agent_token`. Verified against their source at this point of use.
  **We implemented D3 strictly and deliberately did NOT add camelCase tolerance.** Tolerating
  it makes the contract keys optional forever — D10's deliberate tolerance is scoped to the
  *response* key only, where the endpoint does not exist yet and the seam buys a round-trip;
  a state file that already exists needs no seam. **The consequence, stated plainly because it
  is the part they will meet first: `kit.py chat` against a folder the current desktop wrote
  will say "not connected".** That is intended, and it is a loud failure chosen over a
  permanent dual-key tolerance, on the reasoning that a contract whose keys are optional is a
  convention with extra steps.
  **Present this and the `desktop_owned` object form as ONE principle with two instances, not
  as two demands.** The ranked `desktop_owned` section above records the identical trade in
  the identical words — we chose the shape that breaks **loudly and now** over the one that
  degrades **quietly and later**, and rejected a parallel key that would have let both shapes
  live forever. Two separate asks read as us being difficult twice; one position with two
  instances reads as a position, and is also what happened.
- **Your scaffolder will find nothing to substitute for `KIT_VERSION`, and this is a defect in
  your code rather than a difference worth noting — phrase it to them that way.**
  `MANIFEST_TOKENS` (`src/shared/kit/manifest.ts`) enumerates seven tokens, `KIT_VERSION`
  among them. But `templates/agent/cinna-agent.json` carries `"kit_version": "{{KIT_VERSION}}"`,
  and `LocalAgentKitService` substitutes that token **across every rendered member before
  delivery** (`_VERSION_TOKEN`), so in any kit or contract tarball a client actually
  downloads the token is **already resolved**. Six of the seven scaffold tokens are filled by
  the scaffolder; the **seventh is filled by the server before anyone sees it** — an
  asymmetry with nothing anywhere saying so, which is exactly the kind of thing that is
  obvious to whoever built it and invisible to everyone else. It is latent on their side
  today (`MANIFEST_TOKENS` still has no production consumer — `grep -rn MANIFEST_TOKENS src/`
  returns its own declaration and the derived type alias, and nothing else), which is
  precisely why it is worth sending **now**: it is a bug waiting in their scaffolder, cheap
  to fix before it has a caller. Our own `cmd_new` substitution finds nothing there either,
  harmlessly, because the value ends up correct either way — that is why neither host has
  noticed. The contract-documentation half is being written into the kit's `README.md`
  Placeholders section; this list carries the ask.
- Fix `checkIdentity` so an unusable **tool** contract version stops being reported as a
  folder defect. `checkContractCompatibility` (`src/shared/kit/contractVersion.ts:92`)
  returns `unknown` when *either* side is unparseable, and `validator.ts:204-205` routes
  both cases to the same error code, `manifest.contract_version.invalid`. The consequence is
  the reason it is worth one line of their time: **the user is told their folder is broken
  when their app is stale** — precisely inverted from the truth, and it points every
  resulting support conversation at the wrong artefact. `kit.py` already splits the two (an
  unparseable folder version is an error, an unparseable kit version an info saying the gate
  did not run); see Bucket 14d.1.
- Add `credentials/` to their `cloud_import_excludes` (Bucket 2) — without it the two
  `content_hash` walks cover different file sets and never match.
- Fix `asStringArray` so the `desktop_owned` object form parses (`layout.ts:228`).
- Read the desktop state file's path from `layout.desktopOwned()` instead of hard-coding it.
  `src/shared/kit/manifest.ts:150` is `export const DESKTOP_STATE_FILE = 'app-data/desktop.json'`
  and `desktopStatePath` uses it directly (`return join(agentDir, DESKTOP_STATE_FILE)`);
  `layout.desktopOwned()` exists (`layout.ts:86`, `:349`) and has no behavioural reader —
  `grep -rn desktopOwned src/` finds only its own declaration, its projection, and
  `layout.test.ts:23`. **This is the R4 principle on their side of the fence** — the same move
  as our own `desktop_state_file()`, which reads the block rather than the constant beside it
  — so record it as that principle recurring, not as a separate idea. It pairs with the
  `asStringArray` entry above: parsing the object form is what makes reading it possible, and
  reading it is what makes parsing it matter. **Citation correction, reported rather than
  carried:** `desktopStatePath` was handed to this recorder as `:122`; it is at
  `src/main/services/localAgents/desktopStateService.ts:121`, and not in `manifest.ts` at all.
- Drop the closed `enum` on `credentials[].type` in their authored schema (D13).
- Add `.mypy_cache/`, `.ruff_cache/` and `temp/` to their exclude list (D14, Bucket 7).
- Adopt the declared dotenv secret rule (D15, Bucket 7) — this also subsumes §1
  correction 5's `**/*.env` finding, which is folded into D15 rather than tracked
  separately.
- Adopt `secret_files` and apply it to the **hashed** set, not only the copied set (D15
  implementation, Bucket 9a) — implementing it only in `isExcludedFromExport` shrinks their
  hashed set without shrinking ours, guaranteeing a mismatch on adoption.
- Take the D14 exclude-list additions in **paired form** — both the root pattern and its
  `**/`-prefixed sibling — not the `**/`-only form (Bucket 9 / the `**/`-prefixed-directory-
  pattern section above).
- Add `__pycache__/` alongside their existing `**/__pycache__/` — the one entry in their
  authored list that was never paired (Bucket 9 / the section above).
- Add `publications.json` to their `cloud_import_excludes`, root-anchored, as a plain
  string (R4; Bucket 17). Verified absent from
  `resources/cinna-kit-contract/layout.json` today. Nominally this sits inside the D5
  wholesale-re-bundle bullet — take our `layout.json` and the entry arrives with it — but
  the consequence is unstated there and is sharper than the rest of that bullet, so it
  gets its own answer-back paragraph below.
- Fix the schema's `publications[].content_hash` description, which still describes the
  pre-D15 hash (Bucket 10) — a second intentional divergence from their authored schema
  file, alongside D13.
- Point the publication checks at `publications.json` instead of the manifest (R4, Bucket
  16). **This item LEADS with the harm, by ruling — the warning is stronger than the ask,
  and it is what makes a reader act in the right order.**
  **Fixing only `checkPublications` is worse than useless.** The validator goes quiet while
  the card keeps rendering "never published", which removes the only signal a user would
  have had: a partial fix leaves them strictly worse off than the unfixed state. The read
  sites move together or not at all.
  The mechanism behind that, from the R4 review (Bucket 17) — and it is this entry's
  consequence, not a second ask, so it is not duplicated in the standalone section below.
  After R4 `manifest.publications` is **always `undefined`**, because the sole manifest
  writer strips the key on every write. So every read site takes its absent branch at once
  and none of them says anything: `checkPublications` hits `if (publications === undefined)
  return` (`validator.ts:513`) and emits no finding at any severity, and `scannerService`
  falls back to `[]` (`Array.isArray(manifest.publications) ? … : []`). **Net effect: the
  desktop reports every published agent as never published, silently — there is no error
  channel at all, on either the validator or the scanner path.** This is the answer-back's
  own "a filter used where an assertion belongs" shape landing on them from our side: not a
  check that fails, a check that stops running and reports success.
  **The ask itself, stated after the harm because it is the smaller half.**
  `validator.ts:512-540` validates `manifest.publications`, a key R4 removed from the
  manifest; `scannerService.ts:420` reads it, `src/shared/localAgents.ts:249` types it,
  `ReadOnlyCards.tsx:233-246` renders it, and `src/shared/kit/manifest.ts:122` declares it
  (`AgentPublication` itself at `:77`).
  **The checks are right and keep their severity — only the file they read moves.** See its
  own answer-back section below.

**The two authored-schema divergences above (D13, and the `publications[].content_hash`
fix) do not deserve equal billing — rank them, do not list them flat.**

- **`publications[].content_hash` description (D15) — the more urgent.** Their file still
  carries the pre-D15 wording ("`cloud_import_excludes` applied"). A desktop implementing
  `content_hash` from that description alone hashes a set that includes files ours
  withholds, so the two hashes **never agree, silently, with no error on either side** —
  the "unpublished changes forever" failure.
- **`credentials[].type` (D13) — less urgent.** The divergence is a *loosening*: a desktop
  still running the old closed `enum` merely over-rejects an unknown type. Visible, and it
  fails safe.

The generalisable point: a divergence that **loosens** a rule degrades noisily and
recoverably; a divergence that changes **which files a hash covers** degrades silently and
permanently. Both are required desktop-side changes, but they do not deserve equal billing.

- The UTF-16 code-unit seam in segment matching: `?` must consume one code unit. Their
  implementation is already correct and ours was not (Bucket 11) — state it so neither side
  "simplifies" it later.
- The non-UTF-8 filename decoding divergence (Bucket 11) needs a contract decision — agree
  a decoding, or exclude such paths — not an implementation fix on either side.

**Note this one as a convergence rather than an ask:** our unreadable-file refusal now
matches the behaviour their own `ExportTree.unreadable` docstring demands.

**If the answer-back wants a count, generate it from this list at the point of writing —
never assert one mid-document.**

### Answer-back: a filter used where an assertion belongs, degrading to a plausible-looking success

**Instruction to whoever writes the answer-back: give this its own paragraph, not folded
into any of the three ranked items above or into Bucket 8.** The team lead wants it named
as a shape, not left as three separate findings, because the desktop team is about to write
a lot of code against this contract and this is the failure mode they will meet most.

The recurring pattern, found three times independently across this planning and
implementation run: **a filter is used where an assertion belongs, so a missing or wrong
case degrades silently to a plausible-looking success instead of failing loudly.**

- `asStringArray` (`layout.ts:228`) filters `desktop_owned` down to `[]` for the D3 object
  form instead of rejecting it — no throw, no `logger.warn`, no degraded-contract path (see
  the `desktop_owned` section above).
- The stale-snapshot kit test (`docs/` unmounted in the backend container) filters test
  discovery down to whatever the container can see, and reports green having exercised none
  of the working tree (see §0 Bucket 5f).
- `_is_contract_member` filtered the tarball's member set rather than asserting it complete,
  so an incomplete contract — missing `layout.json` and `CONTRACT_VERSION` — still shipped
  as a 200 with a valid ETag (D16, Bucket 8a).

All three are silent, all three produce a result that *looks like* success, and in all
three the cost lands downstream, on whoever consumes the output, with no trace back to the
cause. This is the shape to watch for, on both sides of this contract, more than any single
bug in it.

**Two further instances, from the `PermissionError` round (§0 Bucket 21), and the second is
the cleanest example the run has produced.**

- **A validator that said yes about a subtree it never saw.** `chmod 000` on an agent's
  `.claude/` made `kit.py validate` exit **0**. `_validate_secrets` walks the whole folder
  hunting stray key material with `os.walk`, which **yields nothing for a directory it cannot
  enter** — no error, no marker. The walk filtered out what it could not read and the caller
  read the empty result as an answer, on the one check whose entire job is "there is no
  credential material in this tree". A crash would have been the cheaper outcome.
- **The tempting fix would have MANUFACTURED a finding rather than dropping one.** For the
  fourth condition — an unreadable *file* during validate — the obvious repair was to catch
  `OSError` in the two read helpers and return `""`. That would have reported an unreadable
  `WORKFLOW_PROMPT.md` as an **empty** one: not a lost finding but a **wrong** one, moving the
  reader from neutral to wrong, and strictly worse than the bare errno it replaced. Every
  other instance in this section loses information; **this one invents it.** Say it that way
  to the desktop team — the shape they will meet is not only "a filter swallowed a failure"
  but "a filter turned a failure into a different, plausible finding."

**A second transferable lesson, same section, added by review:** `$` matching before a
final newline in Python but not in JavaScript (Bucket 10, HIGH 2) is a parity break no
amount of shared *data* prevents, because it lives in the host regex engines themselves. D7
fixed the sort seam and left the identical matching seam unproven — same class of problem,
one layer down. Tell the desktop team that **the differential harness, not the contract, is
what catches this class**, and that it is worth their running one too.

**A further transferable lesson, same section, added by the R4 run — and the team lead's
reading is that this is the most instructive single fact of the run, so give it the weight
that implies.** The guard-asymmetry heuristic — *two adjacent statements doing the same job,
one guarded and one not, is itself the finding* — was written into this project's review
checklist (`.claude/commands/cinna-core.code.review.md:102-107`) as a direct response to F6
(Bucket 13). The R4 implementation then reproduced **exactly that defect** in new code, hours
later, written by someone who had been briefed on it: `document.pop("publications")`
unguarded, beside a type-guarded `cloud` removal doing the same job (Bucket 16d).

Two things follow, and both belong in the answer-back because the desktop team is about to
adopt the same kind of heuristic.

- **The checklist entry did not prevent the defect. It made the author able to find it — in
  their own first cut, before review.** Say that plainly rather than selling it as prevention.
  A team that budgets for it as prevention is surprised twice: once when the bug happens
  anyway, and again when they conclude from that the heuristic is worthless. A detection aid
  that fires reliably is worth having; it is simply a different line item, and mis-selling it
  costs more than under-selling it.
- **That it recurred so fast says the shape is not a lapse in attention.** It is what the work
  naturally produces when a guarded line and an unguarded line do the same job in one
  function: the guard is written correctly for the case in front of you, and the neighbour is
  not in front of you. Treating it as carelessness leads to asking people to be more careful,
  which is the one remedy that has now failed twice in this run. Treating it as a shape leads
  to a check applied to the code *surrounding* a change — which is what caught it here.

**A further instance, same section, added by the R4 review-fix round — and this one is about
OUR OWN D15 ask, so it is written as an admission and must be sent as one.** Placement note,
recorded so a later reader knows it was decided rather than defaulted: this was considered for
a standalone ranked section and deliberately folded in here instead, because a new top-level
section would read as a *new* desktop-side ask when D15 is already on the required-changes
list, and because the finding is the fourth instance of exactly the shape this section names.
The evidence trail is Bucket 19; this is the part the desktop must be told.

- **We asked another team to adopt a guarantee that was unreachable in our own
  implementation.** `is_secret_filename` treats an unreadable rule as "assume it protects
  something" and withholds everything, and its docstring promises exactly that. One function
  upstream, `secret_file_rules()` filtered non-dict rules out of the contract list before they
  could ever reach it. The consumer's fail-safe was **unreachable** and the docstring's claim
  was **false** — on the one list whose failure mode is a credential leaving the machine. The
  D15 ask still stands and is still right; the record has to carry this alongside it.
- **It was found because a fix round re-derived the claim rather than trusting the bucket that
  recorded it.** Nothing about the code's appearance flagged it; reading the docstring and
  believing it is what passes.
- **The mutation table in Bucket 11 would have passed, and that is the transferable half.** It
  mutated rule *contents* — `basename_equals: [123]`, an unknown clause key, an empty `match`,
  a `match` that is a list — every one of which survives a `isinstance(rule, dict)` filter and
  reaches the consumer. The defect was in rule **shape**, which does not survive it. **A
  correct test of the wrong layer.** A team adopting D15 must therefore test their own
  adoption **at the shape layer**: feed the rule reader a rules array whose *entries* are the
  wrong kind of thing, and assert the gate still withholds.
- **Say it in those words. Do not write it as "additionally hardened".** The lead's reasoning,
  which belongs in the sent text and not only here: a team that reads the honest version tests
  their adoption at the shape layer; a team that reads the flattering version tests it the way
  we did.
- The fix was to **remove the filter**. Union-with-defaults was considered and rejected: the
  consumer already had the correct documented behaviour, and the fix is to stop suppressing
  it, not to add a second mechanism beside it.

**A further shape, same section, and it now has two independent instances — which is what
promotes it from an anecdote to something worth sending. A WRONG EXPLANATION IS WORSE THAN A
MISSING ONE.** The mechanism, stated so it cannot be softened into "be accurate in comments":
**an unexplained thing leaves a reader uncertain, and uncertainty is what sends someone to go
and check. A falsely explained one stops them looking and leaves them confidently wrong.**
That is the whole distance, and it is why this is not merely one more inaccuracy.

- **Instance one, about a report.** A fix round reported that two deliberately opposed
  fail-safes each carried a note naming the other so nobody would "harmonise" them. The note
  did not exist (Bucket 19j) — and was then asserted a *second* time, independently, in the
  source itself (Bucket 20c). Two independent writers making the same false claim says the
  claim is **attractive**, not that either writer was careless: a protection that *ought* to
  exist is easy to describe as existing, because the sentence writes itself out of the design
  and nothing in the act of writing it ever consults the file.
- **Instance two, about the source, and it is the purest form of the class.**
  `contract_version()`'s docstring in `kit.py` justified its `CONTRACT_VERSION` fallback on
  the grounds that a contract tarball "ships no `kit.json`". **That is false** —
  `INDEX_MEMBER = "kit.json"` is one of `CONTRACT_MEMBERS`, and D2 states it explicitly. **The
  code was correct; only its stated reason was wrong**, so nothing would ever have failed and
  the only casualty was the next reader's belief about what the contract contains — including
  a reader deciding what their own client must fetch. Fixed (§0 Bucket 24b), and fixed in the
  shape that suits the class: the docstring now states the true, narrower thing the fallback
  covers **and forbids the false justification by name**.
- **The remedy is the same move as everything else in this document: make the ARTEFACT
  answer.** A claim that a protection exists must carry the grep output that shows it; a
  claim about what an archive contains must be checked against the member list. A restatement
  is exactly as cheap to produce whether or not it is true. And say it as a property of
  explanations, not as a criticism of authors — a rule that reads as blame gets applied
  selectively, to the rounds someone already distrusts, which are not the rounds where it pays.

**A further shape, same section, and it is the one with the sharpest instruction attached.
WHEN YOU FIND A DEFECT IN A CONSUMER OF SOMETHING YOU PUBLISH, CHECK WHETHER YOU PUBLISHED
THE THING THAT CAUSED IT — because the discovery order will not prompt you to.**

The mechanism, and it is structural rather than moral. We met the desktop's `MANIFEST_TOKENS`
symptom **first**, so the symptom framed the finding: *their* enumeration will find nothing to
substitute for `KIT_VERSION`. Upstream of it sits our own published list, which classifies
`KIT_VERSION` as a scaffold token with no provenance caveat — **their enumeration is the
correct implementation of the list we gave them.** Nothing in the ordinary course of the work
would have prompted anyone to look upstream: the bug was in front of us, in their file, and it
was real.

**Name the outcome that was avoided, plainly, because its shape is the whole lesson.** Had the
connection not surfaced, we would have reported their bug while quietly fixing ours in the
same release — **and it would not have looked like misconduct to anyone involved. It would
have looked like two tidy fixes.** That is what makes the shape worth a section rather than a
reminder: there is no moment at which anyone decides to do the wrong thing, and no participant
who experiences themselves as concealing anything. The failure is in the **order of
discovery**, which nobody chooses.

So the check is mechanical and belongs in the work, not in anyone's judgement: **a defect
found in a consumer of an artefact we publish triggers one question — did our artefact cause
it? — asked before the finding is written up**, because after it is written up the framing is
already set.

**A first-class constraint, never written down before, which has now bitten three times in one
day — and it goes to the desktop, because they will meet it the first time they document a
token in a file we render.** *A rendered member cannot contain a literal `{{TOKEN}}`: the
server substitutes it before delivery, so any member that needs to **discuss** a token must
name it **bare**.* `CHANGELOG.md`, the kit's `README.md` and `templates/agent/README.md` are
all rendered members that describe tokens, and all three must therefore write `NAME`, not
`{{NAME}}`. It is already encoded in `kit.py` — `scaffold_token()` **builds** its strings
rather than writing them out, and its docstring says why — but it had never been stated in
prose anywhere a documentation author would meet it.

**And the second-order effect is the part that earns it a place in this section rather than a
footnote: the constraint silently shaped the original Placeholders paragraph into an awkward
form that DESCRIBED its tokens instead of writing them — and that awkwardness is exactly what
made the wrong list easy to miss.** A constraint that forces prose into an unnatural shape
degrades the reviewability of that prose, and the defect then hides in the unnaturalness. Tell
them the constraint, and tell them that consequence with it; a team told only the rule will
write the same awkward paragraph and inherit the same blind spot.

### Answer-back: R4 moves the file the publication checks read (own paragraph)

**Instruction to whoever writes the answer-back: this gets its own paragraph. Do not fold it
into the required-changes list as a one-liner, and do not write it as "your validator is
wrong" — the framing is the whole content of the item.**

R4 moves `publications[]` out of `cinna-agent.json` into a sibling `publications.json`
(requirements D11, as amended; §0 Bucket 15c-d for why, Bucket 16 for what landed). The
desktop's `checkPublications` (`validator.ts:504-543`) therefore validates a manifest key that
no longer exists there, and the read sites around it — `scannerService.ts:420`,
`src/shared/localAgents.ts:249`, `ReadOnlyCards.tsx:233-246`, the type at
`src/shared/kit/manifest.ts:122` — read it from the same place.

**The checks themselves are right, and they keep their severity.** `kit.py` implements the
same four conditions at the same error severity against the new file, precisely because R4
changed *where* the data lives and not whether checking it is a shared concern
(`kit.py:1145-1194`; Bucket 16f argues the severity rather than picking it). Nothing about
their validation logic needs rethinking — the file it opens does. Say it in that order, or the
item reads as a defect report about code that is correct.

Two riders worth one line each. **The read sites are wider than the validator**, so a fix that
touches only `checkPublications` leaves the scanner and the card reading a key that is now
absent — a folder that has published would render as never published. And **their manifest
type should keep `publications?` as tolerated-and-ignored rather than dropping it outright**:
a folder written before the split still carries it, `kit.py` migrates it on the next manifest
write and never at export (Bucket 16a), so a legacy folder can reach them with the key still
in place.

### Answer-back: the ledger must join their exclude list, or the FIRST publish mismatches (own paragraph)

**Instruction to whoever writes the answer-back: give this its own paragraph. It is
nominally covered by the D5 wholesale-re-bundle ask — take our `layout.json` and
`publications.json` arrives in the list with everything else — but the consequence is not
stated there, and it is the sharpest one in that bullet.**

`publications.json` is not in their `cloud_import_excludes`
(`resources/cinna-kit-contract/layout.json`, verified by loading the file). Their
`collectExportFiles` (`exportTree.ts:50-76`) walks the folder and keeps every path
`layout.isExcludedFromExport` does not reject, and `hashExportFiles` hashes exactly that
list — so the ledger lands in **their hashed set** while our exclude list withholds it from
ours. That is Bucket 9a's guaranteed cross-host mismatch, with one difference that changes
its priority entirely: **the trigger is now the first publish rather than an exotic
`.env.production`.** The file does not exist until something publishes, and it exists
immediately afterwards, on every folder. So the pre-R4 framing — a hash divergence you
reach only if you happen to keep an oddly-named dotenv — no longer holds; after R4 the
divergence is the default state of every published folder, on both hosts, from the moment
publishing starts working.

Two riders. It goes in as a **plain string, root-anchored** — never an object entry, which
`asStringArray` drops without a sound (15d), and never `**/publications.json`, which would
withhold a nested `files/publications.json` that is an ordinary data file and must travel
(verified on our side: root ledger excluded, `files/publications.json` still copied). And
this is **not** a second ask on top of D5 — say it as the D5 bullet's consequence, or they
will fix the list and re-bundle separately and wonder which one was the real request.

### Answer-back: our template made their scaffold's own promise unkeepable (own paragraph)

**Instruction to whoever writes the answer-back: this gets its own paragraph, and the
framing is ruled — lead with what their code promises and with the fact that OURS is what
broke it.** The evidence trail is Bucket 17 HIGH 1 and its citation correction; this is the
pointer and the framing. Do not write it as "your scaffold differs from ours".

1. **The corrected mechanism, which changes the ask.** The desktop does **not**
   token-substitute the manifest as text. `buildManifest`
   (`src/main/services/localAgents/scaffoldService.ts:151-183`) parses the bundled template
   document and sets seven fields on it, and `copyTree` skips the manifest so it is written
   only through that path (`:135`). Its doc comment states the intent in its own words —
   verified verbatim against their source, `scaffoldService.ts:148-149`: *"Build the manifest
   from the template document, so unknown template keys and key order survive exactly as
   `kit.py new` leaves them."* (`MANIFEST_TOKENS`, `src/shared/kit/manifest.ts:128-136`, is a
   real declaration with **no production consumer** — do not cite it as the mechanism; see
   Bucket 17's citation corrections, and `scaffoldService.ts:15-18` for why they rejected
   text substitution.)
2. **Therefore the ask is not "your scaffold differs from ours" but "your scaffold does not
   do what its own comment promises, because our template made that promise unkeepable".**
   Their code faithfully preserves the template it is handed. The template it is handed still
   carries `"publications": []`
   (`resources/cinna-kit-contract/templates/agent/cinna-agent.json:25`), while our `cmd_new`
   routes through `write_manifest`, which strips the key on every write. Preserving the
   template
   *exactly* is precisely what makes their output diverge from ours — the promise is
   unkeepable while the template contradicts the writer.
   **State of our half, re-derived at this point of use rather than quoted from Bucket 17:**
   `grep -n publications docs/local_agent_kit/templates/agent/cinna-agent.json` now returns
   nothing — the in-flight fix round has removed the key from **our** template, so HIGH 1's
   citation of `docs/local_agent_kit/templates/agent/cinna-agent.json:25` no longer resolves
   and Bucket 17's record of it is true as of its own bucket only. **Their bundled copy still
   carries it**, verified now. That is what leaves the divergence live and makes the D5
   re-bundle the whole of the remedy.
3. **State the fault as ours, plainly.** This is more persuasive and it is also more
   accurate: a template that contradicts the schema R4 amended is a cinna-core defect, and it
   is the artefact **we** ship in the contract tarball. The consequence to lead the paragraph
   with is theirs, though: a desktop-scaffolded folder trips our
   manifest-still-carries-`publications` warning immediately, on a folder created seconds
   earlier by the other host implementing the same contract.
4. **Not a new desktop-side ask.** The remedy on their side is the D5 wholesale re-bundle
   already in the required-changes list — take our `templates/` with the corrected template
   and their scaffold's promise becomes keepable again. Do not add it to that list a second
   time.

### Answer-back: two token-egress properties the desktop must check in its OWN HTTP stack (own section)

**Instruction to whoever writes the answer-back: this gets its own top-level section. Do not
rank it here** — rank the sections against each other at the point of writing, per the
standing rule against asserted ordinals.

**Write these as properties of HTTP clients, not as a description of what `kit.py` does.**
The desktop is about to build the other end of this contract **in a different language, with
its own HTTP defaults**. "Your HTTP client may forward the `Authorization` header across a
redirect" is worth more to them than any clause we could add to the contract; "kit.py now uses
a no-redirect opener" teaches them nothing they can act on. The evidence trail is §0 Bucket
20a.

1. **With the guard reverted, the run exited 0 and printed the foreign host's answer.**
   Lead the item with that sentence, in those words, before any mechanism. The reason the
   order matters: the leak's user-visible signature is **a chat that worked**, and **nobody
   investigates a successful chat**. A reader who meets the mechanism first files it as a
   hardening note for later; a reader who meets the symptom first goes and checks their own
   stack. The mechanism comes second, and it is this. **A redirect can carry the bearer token
   to another host.** Python's `urllib` copies the
   request headers onto the redirected request and drops only `Content-Length` and
   `Content-Type` — `Authorization` survives — and for a POST it auto-follows 301/302/303 (as
   a GET). Node's `fetch`/`undici`, Electron's `net`, `axios` and `got` each have their own
   answer to this and **none of them should be assumed**. D10 has no redirect in it, so the
   safe policy on both sides is not "strip the header and follow" but **do not follow at
   all**, and report the 3xx as the non-2xx it is.
2. **A configured proxy can carry it too.** A default client honours `http_proxy` /
   `https_proxy` / `all_proxy` from the environment, and `no_proxy` does not reliably cover
   loopback. On a developer machine with a corporate proxy configured, the agent's bearer
   token and the user's prompt travel through it **on their way to `127.0.0.1`**. A loopback
   API has no business behind a proxy; disable proxying explicitly for this call rather than
   relying on loopback being exempt.

**State that each was proven by a reverted-copy differential, because that is what makes these
findings rather than hardening.** With the guard removed, **the foreign host and the proxy
actually received the bearer token** — observed, not reasoned about. Reproduced twice, once by
the implementing round and once independently while recording it. The redirect leak's outward
appearance is deliberately **not** repeated here — it now opens item 1 instead, because it is
the half a reader acts on and it was doing no work this far down the section. A warning that
reads as hardening gets filed;
this one has a demonstration attached, and the demonstration is the reason to act on it.

**One more, worth a sentence in the same section.** An error string is the cheapest way to
leak a token or a URL: several exception types on this path stringify an attribute of their own
that holds the request URL, and under D3 `api_base_url` is file *contents* a tool never prints.
Our side formats structured fields only — a status line or the underlying socket error, never
the exception whole (§0 Bucket 20a). The same trap exists in every language's exception types.

### Bucket 2 — D6: can `credentials/README.md` and `credentials/.env.example` travel?

**Status: VERIFIED (planning run). Answer: NO for `README.md`. Take the brief's fallback
branch — keep `credentials/` excluded wholesale and record the divergence.**

Evidence:

1. **Nothing server-side rejects a `credentials/` path.** There is no import route to
   reject on: `grep -rn "workspace/upload"` over `backend/app` outside `env-templates`
   has zero backend callers. Env-core's `upload_workspace_tarball`
   (`backend/app/env-templates/app_core_base/core/server/routes.py:1071` →
   `AgentEnvService.extract_workspace_tarball`, `agent_env_service.py:2191`) validates
   only absolute paths, `..` components and post-resolve containment (`2211-2223`) — no
   name denylist. The live-sync path inspects nothing at all.
2. **But `credentials/README.md` collides with a platform-generated file that feeds the
   agent's system prompt.** `AgentEnvService.update_credentials`
   (`backend/app/env-templates/app_core_base/core/server/agent_env_service.py:267`)
   unconditionally rewrites `credentials/credentials.json` (`:305-310`) and
   **overwrites `credentials/README.md`** (`:312-316`), then unlinks every other `*.json`
   in `credentials/` that is not a known service-account id (`:330-337`). Driven by
   env-core `POST /credentials` (`routes.py:798-811`) from
   `CredentialsService.sync_credentials_to_agent_environments`
   (`backend/app/services/credentials/credentials_service.py:1591`) on env start,
   credential update and credential share.
   `PromptGenerator._load_credentials_readme`
   (`backend/app/env-templates/app_core_base/core/server/prompt_generator.py:195-221`)
   **reads `credentials/README.md` into the agent's prompt.** So an imported kit
   `credentials/README.md` would, until the first credential sync, inject local-machine
   instructions (`cp credentials/.env.example credentials/.env`, `git check-ignore -v
   credentials/.env`) into a *cloud* agent's system prompt — then be silently replaced.
3. **`credentials/` is excluded from every bundle snapshot anyway.**
   `BUNDLE_EXCLUDED_TOPLEVEL` (`backend/app/services/environments/workspace_classification.py:57-68`)
   contains `"credentials"` with the comment *"synced separately every env start"*;
   `ENV_MIGRATION_EXTRA` (`:116`) re-adds it only for same-user env-to-env copies.
   `backend/app/services/environments/publish_service.py:9,558` documents the same.
   So even if the two files landed, they would not survive into a published bundle.
4. `credentials/.env.example` on its own is inert in the cloud (nothing reads it, nothing
   deletes it), but travelling alone buys nothing.

**Instruction to the implementer: `layout.json` `cloud_import_excludes` keeps
`credentials/` as a wholesale exclusion. Do not ship the desktop's finer list for this
directory.**

**This divergence is not cosmetic and must be escalated, not just recorded.** The desktop's
authored list lets `credentials/README.md` and `credentials/.env.example` through. If the
two lists differ, the two `content_hash` walks cover different file sets and the hashes
**never** match — the exact "unpublished changes forever" failure §9.3 warns about. So the
answer-back must ask the desktop to add `credentials/` to their `cloud_import_excludes`,
as a **fifth** required change alongside the four in brief §8.

### Bucket 3 — kit-only validator findings, and severity divergences

**Status: VERIFIED (planning run) for the enumeration; Phase 7 confirms each one lands.**

Produced by walking `docs/local_agent_kit/tools/kit.py` and
`/Users/evgenyl/dev/ml-llm/cinna-desktop/src/main/kit/validator.ts` (1094 lines) side by side.

**3a. Kit-only findings — checks `kit.py` has that the desktop's validator does not.
Every one must be a warning (or info), never an error, per §9.2.**

| # | `kit.py` check | Location | Today | After |
|---|---|---|---|---|
| K1 | pyproject ⇄ `workspace_requirements.txt` reconciliation, and its `--fix` | `_validate_requirements:831` | **error** | **warning** (keep `--fix`) |
| K2 | "`description` is still the scaffold placeholder" | `template_description:918` + `_validate_cloud_readiness:934` | warn, **error** under `--cloud-ready` | warning; keep the `--cloud-ready` promotion, but name it kit-only |
| K3 | `EXPECTED_FILES` missing (`README.md`, `AGENTS.md`, `Makefile`, `pyproject.toml`, `workspace_requirements.txt`, `docs/CLI_COMMANDS.yaml`, `scripts/README.md`, `credentials/.env.example`) | `_validate_files:761` | warning | warning (unchanged) — name it kit-only |
| K4 | `missing required file: .gitignore` | `REQUIRED_FILES:106` + `_validate_files:761` | **error** | **warning** — the desktop has no "missing .gitignore" check at all |
| K5 | workflow prompt is empty | `_validate_files:761` | **error** | **warning** — desktop `files.prompt_empty` is a warning |
| K6 | workflow prompt still contains `{{…}}` | `_validate_files:761` | **error** | **warning** — desktop has no equivalent |
| K7 | `credentials/.env` is **tracked by git** | `_validate_secrets:794` | error | warning — desktop has no git-tracked check (it reads `.gitignore` files in-process, never shells out) |
| K8 | `git is unavailable here` | `_validate_secrets:794` | info | info (unchanged) |
| K9 | key-material filename warning (`SECRET_FILENAMES`, `SECRET_GLOBS`) | `_validate_secrets:794` | warning | warning (unchanged) |
| K10 | credential slot carries a forbidden value key (`value`, `secret`, `token`, `api_key`, `client_secret`, `private_key`, `credential_data`, …) | `_validate_credentials:418` | **error** | **stays an error**, and the answer-back **asks the desktop to add it** — demoting a secret-leak guard is the wrong trade. Listed here so the divergence is deliberate, not discovered. |
| K11 | "schedules but no `status` command" | `_validate_commands:868` | warning | warning (unchanged) |
| K12 | manifest `kit_version` predates the current kit | `_validate_cloud_readiness:934` | info | info (unchanged) |
| K13 | the whole `--cloud-ready` promotion mechanism (`blocking = report.error if cloud_ready else report.warn`) | `_validate_cloud_readiness:934` | — | kit-only by construction; the desktop has no analogue. Name it. |

**3b. Severity divergences on checks BOTH validators have. These are the dangerous ones —
a folder the kit calls broken that the desktop happily runs.**

| # | Check | `kit.py` today | Desktop | Action |
|---|---|---|---|---|
| S1 | duplicate credential slot name | error | `manifest.credentials.duplicate` **warning** | demote to warning |
| S2 | unknown credential `type` | error | `manifest.credentials.type_unknown` **warning** | demote to warning (brief §4 already requires this) |
| S3 | handover target folder does not exist next to the agent | error | `manifest.handovers.target_missing` **warning** | demote to warning |
| S4 | catalogued command with no Makefile target | warning | `commands.makefile_target_missing` warning | already aligned — assert it in a test |
| S5 | `example_prompts` empty | warning (error under `--cloud-ready`) | `cloud.example_prompts.missing` warning | aligned outside `--cloud-ready`; K13 covers the gate |

**3c. Pattern divergence — not a severity issue, a false-positive generator.**

| # | Check | `kit.py` | Desktop | Action |
|---|---|---|---|---|
| P1 | CLI command name pattern | `COMMAND_NAME_RE = ^[a-z][a-z0-9_-]{0,31}$` (`kit.py:63`) | `COMMAND_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/` | **Reconcile.** A catalog entry named `Status` or a 40-char name passes the desktop and errors in `kit.py`. Adopt the desktop's pattern as the *error* threshold and keep the stricter lowercase/length rule as a **warning** ("convention: lowercase, ≤32 chars"), so neither side is surprised. |

**3d. Desktop checks `kit.py` lacks. Adding them is optional for §9.2 parity in the
error direction, but each closes a real gap. Ranked.**

| # | Desktop check | Severity | Recommendation |
|---|---|---|---|
| D-1 | `commands.run_reference_unresolved` — `status_refresh_command` is `/run:<name>` but the name is not in `docs/CLI_COMMANDS.yaml` | **error** | **Add.** This is an error on their side; without it `kit.py` passes a folder the desktop refuses — parity failure in the worse direction. |
| D-2 | `commands.unparseable` — a catalog line the mini-YAML reader could not read | **error** | **Add.** `cli_command_names:581` currently drops unreadable lines silently. |
| D-3 | `manifest.example_prompts.too_many` (>20) / `item_too_long` (>500) | error | Add — the schema already declares both bounds; `validate_manifest:355` does not enforce them. |
| D-4 | `manifest.handovers.self` — `target_slug == slug` | warning | Add. |
| D-5 | `cloud.unroutable` — no `router_trigger_prompt` and no `example_prompts` | warning | Add. |
| D-6 | `status.frontmatter_missing` / `status.field_missing` | warning | Add. |
| D-7 | `manifest.features.<key>` value is not a boolean | warning | Add. |
| D-8 | `scripts.catalog_missing` — `scripts/` has `.py` files but no `scripts/README.md` | warning | Add; `_validate_scripts_catalog:901` returns early today. |
| D-9 | `manifest.cloud.deprecated` | info | Add with the `publications[]` work (Phase 7). |

**3e. Note for whoever writes the answer-back.** The desktop emits `manifest.features.type`
at **two different severities** (error when `features` is not an object, warning when a
member value is not a boolean). Any parity harness keyed on `code → severity` will not
round-trip. Likewise their folder-read failures carry a doubled prefix
(`manifest.manifest_invalid_json`, `manifest.manifest_not_found`, …); their UI keys on it,
so it is not to be "fixed".

### Bucket 4 — A1–A8 verdicts

| # | Assumption | Verdict | Evidence |
|---|---|---|---|
| A1 | Guides default the workshop path to `~/Documents/MyAgents` | **CONFIRMED**, and the brief's D8 rider *"nothing else in the kit or the backend references the path"* is **WRONG** | Kit: `START.md:20,24,28,73`, `assistants/codex.md:18`, `guides/11-go-cloud.md:66`, `guides/05-schedules.md:111`. **Also**: `backend/app/services/cli/local_agent_kit_service.py:7` (module docstring), `frontend/src/components/Onboarding/GettingStartedModal.tsx:224` (the "What it creates" tree — a user-facing surface), `.cinna-core-kit/scripts/check_docs_references.py:285` (comment), `docs/application/local_agent_kit/local_agent_kit.md:83,205`, `docs/application/local_agent_kit/local_agent_kit_tech.md:338`. `docs/drafts/local-agent-kit_plan.md` is a historical artefact — leave it. |
| A2 | `cmd_new` substitutes at most `{{NAME}}`, `{{SLUG}}`, `{{KIT_VERSION}}` and knows nothing of `{{DESCRIPTION}}` / `{{ID}}` / `{{CREATED_AT}}` / `{{CONTRACT_VERSION}}` | **PARTLY WRONG** | The second half is right. The first half is not: `substitute_tokens:671` substitutes **`{{name}}` and `{{slug}}` — lowercase** (`cmd_new:708`), across 9 template files. `{{KIT_VERSION}}` is a *platform-render* token filled server-side by `LocalAgentKitService`, not by `substitute_tokens`; `cmd_new` instead overwrites `manifest["kit_version"]` on the parsed dict. The manifest template carries literal `"New Agent"` / `"new-agent"`, not tokens. **Consequence: adopting the desktop's `{{UPPER_SNAKE}}` set is a rename across every template that uses the lowercase pair — see Phase 6.** |
| A3 | The exclude list lives in `kit.json` and is read by `kit_config()` / `cloud_import_excludes()` | **CONFIRMED, and incomplete** | `kit.json` `cloud_import.exclude`, read by `kit_config:227` / `cloud_import_excludes:234`. But there are **three** lists, not one: `DEFAULT_EXCLUDES` (`kit.py:146`, the fallback) and `ALWAYS_EXCLUDE` (`kit.py:137`, appended unconditionally at export, `cmd_export:1370`). Plus a fourth filter that is not a list at all: `is_env_filename` (`kit.py:127`) short-circuits in `cmd_export:1369`. See Phase 4. |
| A4 | Per-check severities are unknown; `_validate_requirements` having a `fix` parameter suggests auto-repair | **ANSWERED** | Full severity map in Bucket 3. `Report` (`kit.py:177`) has four channels — `error` / `warn` / `info` / `fix` — and `fix` is used by exactly one check, `_validate_requirements:831` (`write_workspace_requirements:564`). `report.ok` is `not self.errors`; warnings never fail a run. The `--cloud-ready` flag rebinds a *set* of checks from warn to error (`_validate_cloud_readiness:934`), which has no desktop analogue. |
| A5 | `template_description()` detects an unedited description; `_validate_cloud_readiness` gates the cloud-ready checks | **CONFIRMED** | `template_description:918` reads `templates/agent/cinna-agent.json` `description` ("One sentence describing exactly what this agent does. Rewrite this last, from what you actually built."); `_validate_cloud_readiness:934` compares stripped equality and reports through `blocking`. The desktop has no equivalent — it is kit-only (K2). |
| A6 | `cmd_list` / `_rungs_present` read the `cloud` object for the cloud-state column | **CONFIRMED, with a consequence the handover did not anticipate** | `_rungs_present:1062` appends the `go_cloud` rung on `manifest["cloud"]["agent_id"]`; `cmd_list:1119` reads the same for the `CLOUD` column. **But `cmd_list` also hard-codes the flat cloud workspace**: `root/"Cloud"/".cinna"/"account.json"` and `root/"Cloud"/"agents"`. §6's `Cloud/<host>/` change is therefore a **code** change, not only a docs change. See Phase 9. |
| A7 | The guides tarball's own URL is unknown | **ANSWERED — `/contract.tar.gz` is consistent** | The kit tarball is `{{KIT_BASE_URL}}/kit.tar.gz` where `KIT_BASE_URL` = `<backend_base_url>/api/agent-start` (`local_agent_kit_service.py`, `placeholders()`); served at `/agent-start/kit.tar.gz` and `/api/agent-start/kit.tar.gz` (`routes/local_agent_kit.py:308`), archive rooted at `cinna-kit/`, download filename `cinna-kit.tar.gz`. So `/contract.tar.gz` rooted at `cinna-contract/` with filename `cinna-contract.tar.gz` matches the existing naming exactly. |
| A8 | Every §8 endpoint is unbuilt and unshaped | **CONFIRMED**, plus a material correction | `grep -rn "contract_version\|publications"` over `backend/` **and** `frontend/` returns zero matches; `cinna-agent.json` is never parsed, validated or stored server-side; the manifest `cloud` block is written by `kit.py:1388-1394` and never read back. §8.1 is built by Phase 3 below. **Correction the desktop needs:** §8.3 speaks of "changes to the existing agent-import path", but **there is no server-side agent tree-import endpoint** — the CLI pushes the tree over Mutagen live sync (Bucket 1, finding 3). Their Publish design has to account for that, not for a route change. |

**4i — the source request carries an A9; the requirements brief does not, and the brief is the
narrower document.** Requirements §8 says the answer-back must "confirm or correct each of
**A1–A8**". The handover has **nine**: `cinna_core_handover.md` carries an `A9` row marked
**"New in revision 2"** — that `kit.py`'s secret check pools `.gitignore` lines across directory
scopes, and that its export does not drop `credentials.json` / key material. The brief was written
against revision 1 and its range was never widened when the source grew.

**A9 is answered in the answer-back, and that is correct rather than an overreach.** Its own
wording asks us to check rather than asserting anything, and it is the one assumption where "being
wrong in our favour costs nothing and being wrong the other way ships credentials". Declining to
answer it on the grounds that the brief said A1–A8 would have been the letter of the brief against
its purpose.

**Recorded because the failure mode here is a later tidy-up, not an omission.** A reader who
checks the answer-back against requirements §8 finds nine answers where the brief asked for eight,
and the obvious "correction" is to delete one — removing the answer to the only assumption whose
downside is shipped credentials. This is the same shape as the harmonising edit the standing
checklist warns about: a document that looks inconsistent with its brief invites being made
consistent, and the invitation is strongest exactly where the extra content is load-bearing.
**The brief's range is the stale artefact; the answer-back is right.**

### Bucket 5 — Phase 1 findings (contract data files)

**Status: VERIFIED (Phase 1 implementation run).** Appended by Phase 1; earlier buckets
untouched.

**5a. `desktop_owned` in the D3 object form does NOT parse on the desktop side. This is a
required desktop-side change — a SIXTH one, alongside Bucket 2's `credentials/` request and
the four in brief §8.**

`parseLayout` (`/Users/evgenyl/dev/ml-llm/cinna-desktop/src/main/kit/layout.ts:228`) reads
the block as:

```ts
desktop_owned: asStringArray(doc.desktop_owned).map(normalizeRelPath),
```

and `asStringArray` (`:112`) is
`Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []`.
An array of **objects** therefore filters down to `[]`. The failure is **silent** — no
throw, no `logger.warn`, no degraded-contract path; `KitLayout.desktop_owned` is typed
`string[]` (`:50`) and `LayoutView.desktopOwned()` (`:349`) just returns the empty list.
Their own `src/main/kit/layout.test.ts:23` asserts
`expect(layout.desktopOwned()).toEqual(['app-data/desktop.json'])` and will fail against
the shipped contract.

Blast radius on their side is currently small: the only other reference to the concept is
a comment in `src/main/services/localAgents/desktopStateService.ts:3` — nothing consumes
`desktopOwned()` in a behavioural path yet. So the fix is cheap **now** and gets more
expensive the moment they wire it up.

Per the brief's D3, our side ships the object form regardless — the string array cannot
carry `contract_keys`, which is the entire point of freezing the two keys. The desktop
needs `parseLayout` to accept an entry that is either a string or an object with a `path`,
normalising both to the same internal shape.

**5b. `contractStore.ts` needs no change for our tree — confirmed against the source.**
`isContractTree(root)` (`contractStore.ts:99`) is `kit.json && layout.json` at the root,
and `readVersionAt` (`:129`) reads `kit.json.contract_version` **first**, falling back to
`VERSION` only on a parse failure or a missing key. `kit.json` now carries
`contract_version`, so the `VERSION` fallback never fires and D2's "the workshop `VERSION`
is the kit version, not the contract version" hazard cannot bite. The schema, layout and
template paths are hard-coded constants (`:36-40`) that already match ours.

**5c. Decision recorded for Phase 4: `credentials/.env` is KEPT alongside `credentials/`.**
Phase 1 deviation (a) offered "remove as redundant or keep as belt-and-braces". Kept. It is
redundant twice over (both `credentials/` and `**/.env` subsume it), but a redundant
exclude is a no-op for hash parity while keeping the diff against the desktop's authored
list to exactly one added line in that region. **Phase 4's `DEFAULT_EXCLUDES` must be
content-identical to the shipped `layout.json` list, `credentials/.env` included.**

**5d. Removing `kit.json.cloud_import` is behaviourally inert until Phase 4.**
`cloud_import_excludes()` (`kit.py:234`) falls back to `DEFAULT_EXCLUDES` (`kit.py:146`)
when the block is absent, and that fallback is **content-identical** to the eleven patterns
just removed. So Phase 1 changes no export behaviour, which is what makes it reviewable on
its own. Confirmed by `tests/unit/test_local_kit_tool.py` (39 tests) passing against the
live tree.

**5e. Phase 11 pre-check, done early because an extensionless file is exactly what a
glob-based sync drops: both new files ride the sync.** `_is_publishable`
(`.cinna-core-kit/scripts/sync_platform_knowledge.py:148`) is **allow-by-default** — it
rejects only `KIT_DENY_NAMES` (`.DS_Store`, `Thumbs.db`), `KIT_DENY_SUFFIXES`
(`.key .pem .p12 .pfx .crt .swp .orig`) and names containing `.env` that do not end in
`.example`. `layout.json` and `CONTRACT_VERSION` match none of those, and the walk is
`rglob("*")` over every regular file. No allowlist to extend. Phase 11 still runs the real
`make sync-platform-knowledge`.

**5f. TRAP for every later phase that runs the kit tests in Docker — a vacuous pass.**
`_find_kit_dir()` (`backend/tests/unit/test_local_kit_tool.py:35`) resolves in the order
`$LOCAL_AGENT_KIT_DIR` → a repo `docs/local_agent_kit/` → the synced snapshot at
`backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/`.
**`docs/` is not mounted into the backend container**, so
`docker compose exec backend python -m pytest tests/unit/test_local_kit_tool.py` silently
tests the **stale snapshot**, not the working tree — it reported 39 green against a
`local-kit/` that had no `layout.json` and no `CONTRACT_VERSION` at all. Run it as:

```
docker compose cp docs/local_agent_kit backend:/tmp/kit_phase1
docker compose exec -T -e LOCAL_AGENT_KIT_DIR=/tmp/kit_phase1 backend \
  python -m pytest tests/unit/test_local_kit_tool.py -q
```

re-copying after every edit. Phases 4-9 all edit `kit.py`; every one of them is exposed.

**5g. One stale reference Phase 1 created and deliberately did not fix (out of its file
scope).** `docs/local_agent_kit/guides/11-go-cloud.md:174` still reads *"applying
kit.json's cloud_import.exclude list"*, naming a key that no longer exists. Phase 4 bullet 6
already updates the same wording in `cmd_export`'s error string; the guide line belongs with
it or with Phase 10.


---

### Bucket 6 — Phase 2 findings (manifest schema)

**Status: VERIFIED (Phase 2 implementation run) — except 6b, which is FALSE; the
correction is appended to 6b below, and Bucket 14g and Bucket 15 carry the wider record.**
Appended by Phase 2; Buckets 1–5 untouched.

**6a. `$id` verdict: confirmed identical.** Our old file, the desktop's authored schema, and
the new merged file all carry `https://cinna.dev/schema/cinna-agent.schema.json`. D11's
"confirm and keep" resolved as a no-op.

**6b. Correction to Phase 2's own "Reviewable when" note: the Phase 6 coupling is benign in
both directions.** `test_template_manifest_matches_the_shipped_schema` does not break and
does not need Phase 6 to stay green — the shipped template (`schema_version: 1`, no
`contract_version`/`id`) lands in the legacy-exemption branch of the new top-level `allOf`
and validates unchanged, and the Phase 6 template (`contract_version` + `id`, no
`schema_version`) takes the `else` branch and validates too. Verified empirically against a
12-case behaviour matrix.

**6b — CORRECTED (ruled by the team lead; original text above left standing, unsoftened,
because the correction is only legible against it).** The claim is **false**. The test does
break, and it breaks on the template Phase 6 actually landed:

```
ValidationError: '{{SLUG}}' does not match '^[a-z0-9][a-z0-9-]{1,62}$'
```

That template ships unsubstituted scaffold tokens — `"contract_version":
"{{CONTRACT_VERSION}}"`, `"id": "{{ID}}"`, `"slug": "{{SLUG}}"` — and the test validates
*that file*. `jsonschema` rejects it on `slug`, **before any identity branch is reached**,
so the failure has nothing to do with the identity `allOf` the matrix was exercising. The
stdlib half fails identically and for the same reason.

**Why it was wrong is the part that transfers, and it is not "the matrix was sloppy".** The
12-case matrix was a real instrument, correctly built and correctly run. It was pointed at a
**hypothetical Phase 6 template** — the one the plan described — rather than at the one that
landed. A sound measurement of the wrong artefact. See Bucket 15f, where this is recorded as
its own instrument-failure shape, and Bucket 14g, which found it from the other side.

**Not fixed here, because it is a decision rather than a bug.** Either the test validates a
*scaffolded* manifest instead of the template (Phase 12), or the template ships
substitutable-but-valid placeholder values (Phase 6/10). Loosening the schema patterns to
admit token strings is the one option that must not be taken: it would stop the schema
rejecting a folder whose scaffold never ran, which is exactly what it exists to catch.

**6c. Side effect of dropping `const: 1` from `schema_version`.** `schema_version: 2` now
parses where it was previously a schema error. Intended — nothing branches on the value —
but worth having written down rather than discovered later.

**6d. Desktop schema ⇄ desktop validator inconsistencies, for the answer-back's benefit —
mirror images of Bucket 3d.**

- Bounds their **validator enforces that their schema does not declare**: `checkIdentity`
  runs `contract_version` (`validator.ts:189`) and `id` (`:209`) through
  `checkString(..., 64, ...)`, yielding `…too_long` findings. The schema declares no
  `maxLength` on either. Inert in practice, but undocumented.
- Bounds the **schema declares that their validator does not enforce**:
  `credentials[].description` and `handovers[].description` both carry `maxLength: 2000`;
  `validator.ts:387-393` and `:481-487` check only `typeof !== 'string'`. Note the contrast
  with Bucket 3d's D-3: the desktop *does* enforce the `example_prompts` bounds (`too_many`
  >20, `item_too_long` >500, `:260-279`) that our `validate_manifest` skips — so D-3 is a
  one-sided gap in their favour, while these two are one-sided in ours.
- **`features`:** the schema is `additionalProperties: true` with no member-type
  constraint, so `features.foo: "x"` is schema-valid; their validator warns
  `manifest.features.type`. Consistent with Bucket 3e.
- **`runtime.credential`'s secret guard is code-only on both sides** — the schema states it
  in prose only. The exact rule for Phase 7 to match:
  `/^(sk-|sk_|ghp_|gho_|xox[baprs]-|AIza|AKIA)/` **or** length > 200 → error
  `manifest.runtime.credential_looks_like_secret` (`validator.ts:309-316`).
- **Aligned, assert rather than assume** (flag for Phase 12): `prompts` unknown key is
  `additionalProperties: false` in the schema and `manifest.prompts.unknown_key` **error**
  in their validator; `slug` is 2–63 in both.

**6e. Note for the answer-back: Bucket 3b/S2 predates D13.** S2's demotion of `kit.py`'s
unknown-credential-type check to a warning was written before D13 existed, as a
two-against-one compromise between the two validators (kit.py said error, the desktop said
warning). With D13 dropping the schema's closed `enum`, S2 is now the fully consistent
position — schema, `kit.py` and the desktop's validator all agree an unrecognised type is a
warning, not an error, rather than a compromise. The answer-back should say so.

### Bucket 7 — Phase 4 & 5 findings (`kit.py` excludes, matcher, `content_hash`)

**Status: VERIFIED (Phase 4 & 5 implementation run) — except F6, found later; see Bucket
13.** Appended by Phase 4/5; Buckets 1–6 untouched.

**7a. D14 — `.mypy_cache/`, `.ruff_cache/` and `temp/` join the shared exclude list.**
Phase 4 found all three now travel: the first two were dropped silently by `iter_files`'s
`SKIP_DIRS`, `temp/` was in the old `DEFAULT_EXCLUDES`, and the contract list names none of
them. The implementer read this as parity with the desktop's list; the team lead overruled
that framing — parity is the mechanism, not the goal, and "both sides upload a
`.mypy_cache`" is an agreement to do something bad in unison, not evidence the omission is
safe. `temp/` is the worst of the three: it was already excluded, so dropping it is a
regression introduced under cover of alignment, and the most likely of the three to hold
something unintended. Decision: add all three to the shared exclude list.

**7b. D15 — the dotenv secret rule becomes declared data in `layout.json`.** Phase 4
bullet 5 told the implementer to delete the `is_env_filename` short-circuit; they measured
the premise before acting on it and found it false — the contract list *enumerates* dotenv
suffixes, so `.env.production` / `.env.staging` / `.env.development` already travel at any
depth on both sides. The guard therefore stays, but moves out of `kit.py` and into
`layout.json` as a declared block, in the spirit of `local_command_runner` — a hand-rolled
secret filter living in `kit.py` re-creates exactly the drift D6 and `layout.json` exist to
end. Semantics: a basename that is `.env`, starts with `.env.`, or ends with `.env` is a
secret and never travels, **unless** it ends with a declared allowed suffix (`.example`,
`.sample`, `.template`). **D15 subsumes §1 correction 5's `**/*.env` finding** — that
correction is folded into D15, not tracked as a separate outstanding item (see the note
appended to §1 below).

**7c. `UNREADABLE_MARKER` carries TWO NULs, not one.** `exportTree.ts` sets it to
`'\0unreadable'` and emits `` `${rel}\0${hash}\n` ``, so the emitted line is
`<rel> NUL NUL "unreadable" LF`. The handover's §9.3 prose describes a single NUL. Proven
by construction, not by inference — anyone reimplementing §9.3 from the prose alone gets
this wrong.

**7d. The UTF-16 sort key is proven load-bearing, not cosmetic.** A tree containing
`emoji/😀.md` (U+1F600) yields *different digests* under Python's default `sorted()` versus
`key=lambda p: p.encode("utf-16-be")`; the UTF-16 key is the one that matches the desktop's
output. D7's insistence on the UTF-16 sort key was correct and is not a portability nicety.

**7e. Differential verification result.** The desktop's `matchesPattern`, extracted
read-only into a scratch harness, was run against 73 patterns × 72 paths — **5256 cases, 0
mismatches**. `collectExportFiles` + `hashExportFiles` were run over a 27-file synthetic
tree including symlinks, a chmod-000 file and non-BMP names: file list identical,
`contentHash` byte-identical; an empty tree hashes to `sha256:e3b0c442…b855` on both sides.

**7f. `ALWAYS_EXCLUDE` is provably redundant against the shipped list** — measured, not
assumed; nothing uncovered by removing it. Phase 12's assertion to that effect will hold as
written.

**7g. Phase 4 bullet 4's two "verify before shipping" items, both confirmed inert.**
Dropping the agent-root `Makefile` from the tree: `AgentStatusService._run_refresh_command`
(`backend/app/services/agents/agent_status_service.py:170`) resolves `/run:<name>` through
`environment.cli_commands_parsed` and executes the YAML command via
`AgentEnvConnector.exec_command`, never through `make`. Dropping `files/.gitkeep`: env-core
tolerates a missing `files/` directory — `get_workspace_info` only reports `has_files_dir`,
and the tree scan (`agent_env_service.py:742`) skips a non-existent folder rather than
erroring.

**7h. Phase 5 bullet 6's `cloud` → `publications[]` migration is unobservable by
construction.** The same bullet clears both `cloud` and resets `publications` to `[]` in
the exported copy, so only the observable half was implemented — there is nothing left in
the export to migrate *from* by the time migration would run. The real migration belongs
where a *source* manifest is written and read back, not where it is exported: Phase 6/7.
Flagged there. **Later correction — see Bucket 13:** this paragraph is the closest anyone
came to F6 and remains true as written, but the `cloud` clear it describes was
*unconditional*, so on a manifest without a `cloud` key export **added** one rather than
clearing it. The question asked here was whether the migration is observable, not whether
the clear is safe on a manifest that has nothing to clear.

**7i. `_excluded_for_report` is a reporting-only second pass.** It descends into excluded
directories — so, for example, `credentials/.env` can still be *named* in a report — while
still pruning `SKIP_DIRS`. It reports; it never decides what travels. Called out here for
review attention, not because it is wrong.

### Bucket 8 — Phase 3 findings (backend contract tarball and version endpoints)

**Status: VERIFIED (Phase 3 implementation run).** Appended by Phase 3; Buckets 1–7
untouched.

**8a. D16 — the asymmetric-degradation finding, settled.** `/contract/version` fails loud
(503) while `/contract.tar.gz` degraded **silently** — 200 with a truncated archive and a
valid ETag — because the member selector was a *filter*, not an assertion. Live proof: on
the current instance `/contract/version` 503s while `/contract.tar.gz` serves a 33-member
archive missing `layout.json` and `CONTRACT_VERSION`.

Ruling: guard inside `get_versioned_contract_tarball()` **only** — not `_build_or_cached`,
not the route — because the shared build path would 503 the entire anonymous surface
(`START.md`, `/version`, `kit.tar.gz`) on any snapshot lacking contract files, breaking a
surface designed to degrade gracefully. The load-bearing member set is `kit.json` +
`layout.json` + `CONTRACT_VERSION`: the first two are the desktop's identity pair, the
third is what D2 rests the version fallback on; `schema/` and `templates/` being thin is a
content problem and must not be dressed up as an identity failure. **The
version-agreement check folds into the same guard** — verify `CONTRACT_VERSION` ==
`kit.json.contract_version` == `layout.json.contract_version` and 503 on disagreement (this
resolves Phase 3's finding 3; a Phase 12 invariant test only helps if someone runs it before
shipping).

**Phase 12 note:** its tests must be written to D16's behaviour, not to the pre-D16
behaviour this plan's Phase 3 section currently describes.

**D16 amended — cover `/contract/version` too.** Recorded as an amendment, not a new
decision: both contract representations now degrade together. The boundary, stated
explicitly: D16's narrow scoping protects the **anonymous kit surface** (`START.md`,
`/version`, `kit.tar.gz`, `/kit/{path}`), which must serve a stranger regardless of
contract health; `/contract/version` is not part of that surface but the contract's second
representation. Verified: with `layout.json` deleted, all four kit-surface paths still
answer 200 while both contract representations 503, and the thin-`schema/` control still
200s on both. The decisive case is `CONTRACT_VERSION=2.0.0` with the others at `1.0.0`: the
endpoint the desktop *polls* publishes an unvalidated number that §4's major-version gate
keys on — and the stale direction is worse, because a false *compatibility* fails silently
where a false incompatibility at least stops. Also record finding 8's fix: the verdict is
computed once per snapshot inside the existing critical section, not per request.

**8b. The contract ships `templates/agent/.claude/settings.local.json`** — a Claude Code
permissions file — so the desktop will materialise a `.claude/` directory it does not own
in every scaffold. Name it explicitly to the desktop; from their side this reads as a bug
unless it is stated.

**8c. The contract is self-describing on scaffold ignore files.** The three dotless files
that ride it (`templates/agent/gitignore`, `templates/agent/app-data/cache/gitignore`,
`templates/root/gitignore`) are exactly the pairs `layout.json.scaffold_ignore_files`
declares, and `templates/agent/credentials/.gitignore` correctly stays dotted and out of
that list.

**8d. A7/§1.7 confirmed empirically.** Naming matches the kit exactly (`cinna-contract/`
root, `cinna-contract.tar.gz`), and the tarball ships no `VERSION`, so
`_locate_extracted_kit` (which requires `kit.json` AND `VERSION`) can never locate a
contract tree, as §1.7 already noted.

**8e. Phase 3's "Reviewable when" cannot be signed off on a deployed instance until Phase
11 runs the sync.** The snapshot differs from `docs/local_agent_kit/` by exactly the
missing `CONTRACT_VERSION` and `layout.json`, plus a stale harmless
`tools/__pycache__/kit.cpython-313.pyc`. Flagged against Phase 11.

**8f. The non-vacuity exemplar (see the standing checklist).** Phase 3 could not use
`LOCAL_AGENT_KIT_DIR` at all — the service never reads it — so it monkeypatched
`local_agent_kit_service.local_kit_dir` to a container-local copy, printed both trees side
by side before serving (`snapshot layout.json=False CONTRACT_VERSION=False` vs
`working copy True/True`), and hit the live server for contrast (503 + 33 members vs its
own 35). It could not copy the working tree directly into the snapshot path either:
`docker-compose.override.yml` bind-mounts `./backend/app:/app/app`, so that write would
have landed in the repo, not a container-local scratch copy.

### Bucket 9 — Phase 4/5 D14 & D15 implementation findings

**Status: VERIFIED (Phase 4/5 D14/D15 implementation run).** Appended by Phase 4/5; Buckets
1–8 untouched.

**9a. The hash-parity position on D15 was reversed, and the reversal is the substantive
finding.** The implementer's first analysis (copy-only guard, "strict subset is the safe
direction") checked cross-host agreement on a *fixed* tree — true, but the wrong invariant.
It missed stability **over time**: with a copy-only guard, editing a `.env.production`
moves the local hash while the recorded publication hash can never move to meet it, because
no publish will ever include that file. The host then reports "N unpublished changes" that
no publish can clear — §9.3's exact failure, reached from the other direction, and
**unfalsifiable from the user's side**: they see a number that never reaches zero. A second
failure mode is created by D15 itself: once the rule is contract data, the desktop's
natural implementation site is inside `isExcludedFromExport`, which feeds *their* hash —
their hashed set shrinks, ours does not, guaranteed mismatch on adoption. **Resolution: the
secret filter applies to the single walk (`collect_export_files`), so copy set == hashed
set == the desktop's set.** The contract `notes` state this as a MUST so the desktop
implements it on the same side. Verified by extending the differential harness to model the
post-D15 desktop: identical file list and `content_hash`.

**9b. The fail-safe direction is a parameter, not a constant.** An unevaluable `match`
clause returns `True`, an unevaluable `unless` clause returns `False` — both resolving
toward "secret". A single constant would have made a future contract's unknown clause ship
a credential. Worth preserving in review, since it reads as over-engineering until exactly
that happens.

**9c. `secret_files` shape.** `rules` is a list so a second rule (key material, say) is an
append rather than a schema change; clauses are `basename_equals` / `basename_prefix` /
`basename_suffix`, tested against the basename at any depth; `unless` rather than `except`
because the latter is a Python keyword. The rule applies to **directories** as well as
files, so a `secrets.env/` folder is withheld wholesale rather than descended into.

**9d. The redundant patterns stay, deliberately.** `**/.env`, `**/.env.local`, `**/*.env`
remain in `cloud_import_excludes` even though `secret_files` subsumes them: a redundant
exclude is a measured no-op for hash parity; a list entry is a one-line merge on the
desktop's side while a rule block is code, so the patterns are the only protection during
the window where they have taken D14 but not D15; and Phase 1 made the same call for
`credentials/.env` (Bucket 5c). The `notes` name the block as the authority and the
patterns as the subset.

**9e. `is_env_filename` stays unchanged** and is now the only user of the wide reading —
`_validate_secrets` wants it, since an `.env.example` outside `credentials/` is misplaced
by convention regardless of whether it holds a value. Narrowing it there would silently
drop a `validate` error; that is Phase 7's territory.

**9f. Re-proofs at the new length.** `DEFAULT_EXCLUDES` ≡ `layout.json`
`cloud_import_excludes`: True, **40 entries**, same order. **Later correction — see Bucket
16h:** the equality and the ordering still hold, but the length does not; R4 added
`publications.json` and the list is now **41 entries**. The 40 was true when it was written
and is left standing as the record of that run rather than overwritten — re-derive the
current figure, do not quote either one forward. `DEFAULT_SECRET_FILE_RULES` ≡
`layout.json` `secret_files.rules`: True. Matcher differential vs their `matchesPattern`:
**4608 cases, 0 mismatches**. `ALWAYS_EXCLUDE` still provably redundant. Suite: 39 passed.

**9g. Non-vacuity — the strongest evidence in the run so far.** A **data-only mutation**:
deleting `unless` from a *copy* of `layout.json`, with `kit.py` byte-identical, flipped
`is_secret_filename(".env.example")` to `True`. That proves the reader genuinely consults
the contract rather than a hard-coded copy — the exact property D15 exists to buy. Plus the
code-mutation proof through the real suite (one line reintroducing pre-Phase-4 basename
matching → the export test fails; unmutated → 39 pass).

### Bucket 10 — Phase 1-5 review findings

**Status: VERIFIED (review pass).** Appended by review; Buckets 1-9 untouched.

**HIGH 1 — `secret_files` failed OPEN on an unrecognised rule *shape*, D15's single
forbidden direction.** `kit.py:359-392`. The fail-safe was applied correctly to an unknown
clause key *inside* a present `match`/`unless`, but never to the rule shape itself: `match`
renamed, `match` a list rather than an object, or a known clause carrying unusable values
(e.g. `basename_equals: [123]`) all cause `_secret_clause_hits` to return `False`, the loop
to `continue`, and the rule to become a **silent no-op** — `.env`, `.env.production` and
`prod.env` all flip from secret to not-secret. The `cloud_import_excludes` belt-and-braces
deliberately kept for that adoption window does **not** cover it: verified,
`.env.production`, `config/.env.staging` and `.env.prod` are all
`excluded-by-patterns-alone = False`.

Record the lead's framing, because it is the transferable lesson: *the reason it survived
is instructive — the fail-safe was implemented correctly at the level someone was thinking
about, and never lifted to the level above it. A contract 1.1.0 that restructures the rule
would silently disarm the gate against every older `kit.py` in the field. That is a leak
path found in the mechanism built to prevent leaks, which is exactly why the mutation
testing is worth what it costs.* Fix dispatched: lift the fail-safe from clause level to
rule level.

**HIGH 2 — matcher parity break on trailing newlines.** `kit.py:1483`: Python's `$` matches
before a final `\n`, JavaScript's (no `m` flag) does not. 12 mismatches in a 4071-case
differential, 11 of this class. A file named `notes.md\n` (legal on macOS and Linux) is
withheld by us and uploaded by them → different file lists → different `content_hash` →
"1 unpublished change" no publish can clear. Fix: `\Z` or `re.fullmatch`.

**MEDIUM** — `?` matches one code point in Python, one UTF-16 code unit in JS (`'?.md'` vs
`'😀.md'`); latent, but `?` is declared semantics in both `layout.json`'s notes and the
`matches_pattern` docstring. **MEDIUM** — unreadable files are silent in `content_hash` and
an uncaught `PermissionError` in the copy loop; the desktop has an explicit `unreadable[]`
refuse-to-publish path and we have no counterpart, so `export --hash` can print a plausible
digest of a tree it never fully read. **LOW** — a non-UTF-8 filename raises
`UnicodeEncodeError` in both `_utf16_sort_key` and the hash-line encode, uncaught; live on
Linux, where a cinna-cli import may run. **LOW** — the schema's
`publications[].content_hash` description still describes the pre-D15 hash. **LOW** —
`_assert_contract_coherent` re-parsed `kit.json`+`layout.json` per request against the
module's "never touched per-request" promise. **NITs** — unclosed `ScandirIterator`; a
skipped symlink appears in neither the copy nor the excluded-summary.

**What held up under adversarial probing** (worth recording so a later reader does not
re-verify it): D16's guard across 11 mutation cases with the thin-`schema/` negative
control; one cache/one lock/one render with a byte-identical rebuild; the 35-member
contract subset exact; ETags keyed on `kit_version` with distinct per-representation tags;
`DEFAULT_EXCLUDES` ≡ `layout.json` (40, ordered — **the equality and the order held and
still hold; the 40 is superseded by 41 at Bucket 16h**); `DEFAULT_SECRET_FILE_RULES` ≡
`secret_files.rules`; `ALWAYS_EXCLUDE` redundancy; `normalize_rel_path` byte-equivalence;
`**/`-prefixed directory patterns confirmed unable to match at root; the two-NUL
`UNREADABLE_MARKER`; the secret rule genuinely read from contract data; stdlib-only intact;
and the Phase 2 schema diff containing rows 1-10 and nothing else.

**Notes for later phases, from this review pass:**

- **Phase 11 is load-bearing, not hygiene:** the running backend's snapshot has neither
  `layout.json` nor `CONTRACT_VERSION`, so both contract endpoints 503 on this instance
  today. Correct per D16, but the sync must land before any desktop is pointed here.
- **Phase 12's matcher table must include the newline and `?` cases** — the 4608-case
  harness passed because neither input class was in it.
- **Phase 7:** the shipped schema now *states* an unrecognised credential `type` is
  reported as a warning; if `validate` still errors after Phase 7, the bundled contract
  artefact is factually wrong.
- **Phase 6/7:** a future contract renaming a clause key inside `unless` flips
  `.env.example` to secret (the intended fail-safe direction). The scaffold ships
  `credentials/.env.example`, so a `new` → `export` round trip would quietly change
  composition — pin it with a test.
- **Phase 8/6:** `main()` catching only `KitError`/`KeyboardInterrupt` will bite again the
  moment a network or filesystem error reaches the top level.

### Bucket 11 — Phase 4/5 review-fix round

**Status: VERIFIED (review-fix implementation run) — except HIGH 1's mutation table, which
is PARTIALLY INERT against the layer it was read as covering; found later, see Bucket 19.**
Appended by the fix round following
Bucket 10; all seven findings fixed, none disputed. Buckets 1-10 untouched.

**HIGH 1 fixed — the fail-safe is now a rule-shape rule, not just a clause-key rule.** The
original bug: `rule.get("match")` returning a non-dict hit a hard `return False` that never
consulted `on_unknown`. "Cannot evaluate" is now broad and lands on `on_unknown` in every
case — clause absent, not an object, an *empty* object, an unknown key, or a known key with
no usable string; and a rule that is not a dict returns secret immediately. Notably no
special case was needed for the common shape: an absent `unless` is unevaluable *and* means
"no exception", and `on_unknown` is `False` there, so the general rule already gives the
right answer.

Re-run mutation table — every row now lands on secret for `.env`, `.env.production`,
`prod.env`: `match` renamed, `match` a list, `basename_equals: [123]`, unknown key inside
`match`, `match` an empty object, `match` absent, rule a string, `unless` a list, `unless`
with an unknown key. Two deliberate right-hand-column behaviours: where `match` is
unreadable but `unless` parses, `.env.example` still travels (the same contract explicitly
declares that suffix non-secret, so permitting it is not a leak); where `unless` itself is
unreadable, it stops travelling. `rules=[]` remains the one way the gate is off — an
explicit caller choice, not a contract defect, now stated in the docstring. **A contract
this build cannot read withholds everything**, which is loud and recoverable via
`kit.py refresh` rather than silent.

**HIGH 2 fixed — and fixed better than the one-character suggestion.** The segment regex is
now compiled **unanchored** and matched with `fullmatch`, rather than `\Z` inside `^…$`:
`fullmatch` cannot be defeated by a trailing newline *and* cannot be re-broken by someone
appending to the pattern source, which `\Z` in a string concatenation can. Verified `[^/]`
matches `\n` in both languages, so `*` was never affected — only the anchor.

**MEDIUM `?` fixed.** New `_to_code_units()` re-expresses both pattern and path segment as
UTF-16 code units (non-BMP → surrogate pair, one Python character each) before matching,
with an `isascii()` fast path. `?` now consumes one code unit exactly as JS does.

**MEDIUM unreadable files fixed, both halves, plus three deliberate extras.**
`hash_export_files(agent_dir, files) -> (hash, unreadable)` mirrors their `hashExportFiles`
return shape; `content_hash` is a thin str-returning wrapper so the signature Phase 12 will
test is unchanged. `cmd_export` refuses on a non-empty unreadable list. Extras, all ruled
acceptable by the coordinator: the walk and hash run **before** `destination.mkdir`, so a
refusal leaves nothing half-written; **`--force` does not waive it** (`--force` waives
validation findings, not a hash that does not describe the bytes) which also removes the
`PermissionError` path rather than papering it; and an `OSError` between hash and copy
becomes a `KitError` naming the file. Verified against `chmod 000`: refusal exit 1, no
traceback, no destination, two-NUL marker digest still byte-exact.

**LOW non-UTF-8 filenames fixed, with a parity limit that is a contract question.**
`surrogatepass` on both `_utf16_sort_key` and the hash-line encode; no raise, mixed lists
still sort. **But parity is unreachable for such a name regardless**: Python decodes with
`surrogateescape` (`\udcff`), Node with lossy U+FFFD replacement — different strings,
different digests, whatever we encode with. `surrogatepass` buys determinism and no crash,
not agreement. The real fix is a contract decision (agree a decoding, or exclude such
paths). Recorded as an answer-back item needing a decision, not an implementation ask — see
the required-changes list above.

**NITs fixed.** `with os.scandir(...)` (zero `ResourceWarning` under
`simplefilter("always")`). And the symlink omission was **worse than reported**:
`_excluded_for_report` is now its own `os.walk` because it needed `dirnames` to see
symlinked *directories*, which `iter_files` never yields at all — so a symlinked dir was
invisible even to the file-level walk. Symlinked files and dirs are now named with a reason
(`knowledge/shared/ (symlink — never travels)`); the skip in `collect_export_files` is
untouched, and the line is marked kit-only.

**Re-proofs.** Matcher differential rebuilt with trailing-newline names, nested newlines,
`?`/`??`/`???` patterns and non-BMP vs BMP paths: **6557 cases, 0 mismatches**. Targeted
rows recorded: `README.md` vs `README.md\n` → both False; `?.md` vs `😀.md` → both False;
`??.md` vs `😀.md` → both True; `*.md` vs `😀.md` → both True. Hash differential on a
24-file tree incl. newline-named files, symlinks, non-BMP names and `secrets.env/`: file
list and `content_hash` identical to the D14+D15-modelled desktop. `DEFAULT_EXCLUDES` ≡
`layout.json` (40): True. `DEFAULT_SECRET_FILE_RULES` ≡ `secret_files.rules`: True. Suite
39 passed.

**Non-vacuity — a new strongest form: the reverted-copy differential.** A kit copy with
`match` renamed in the *copy's* `layout.json` AND only the two HIGH code fixes reverted in
the copy. On identical mutated data: fixed `kit.py` → all three dotenv shapes **secret**;
pre-fix `kit.py` → all three **LEAK**. Same copy with the `$` regression restored:
`README.md` vs `README.md\n` → desktop 0, fixed 0, pre-fix **1**. The reverted copy
reproduces the reviewer's table exactly, which is what proves the fixes are load-bearing
rather than incidental. Worth recording as a technique: it verifies not just that the new
code works but that the *specific defect* is what the fix addresses.

### Bucket 12 — backend D16-amended round

**Status: VERIFIED (backend implementation run).** Appended after Bucket 11; Buckets 1-11
untouched.

**The boundary is now structurally enforced, not conventional.** The raising
`_assert_contract_coherent` was split into a **pure** `_contract_defect_reason(rendered) ->
str | None` (decides; does not raise, does not log) plus a pure
`_read_contract_version(rendered) -> str | None`. The verdict is carried as **data** on the
cached build (`_KitBuild.contract_defect`), and one gate —
`_require_serviceable_contract(build)` — is called by `get_versioned_contract_tarball()`,
`get_contract_version_payload()` and `get_contract_version()`, **and by nothing else**. That
is what makes the kit surface's exemption structural: it can hold the verdict and simply
never consult it. The gate's docstring states the boundary in both directions and closes
with *"Do not narrow this to one representation, and do not widen it to the kit surface.
Both directions have been tried and ruled on."* The route file gained only a comment; both
handlers inherit the behaviour from the accessors they already call.

**The cache became a `NamedTuple`** (`version`, `rendered`, `tarball`, `contract_tarball`,
`contract_defect`) rather than a six-wide positional tuple, because every accessor was
unpacking with a row of underscores — the implementer's reasoning, worth keeping: *that is
exactly how a later edit reads the contract tarball as the kit one.* Still one cache, one
lock, one render; the verdict is computed in the same critical section as both tarballs.
Measured, not assumed: **12 contract requests produced 1 evaluation**.

**Diagnostics.** Specifics are logged once per snapshot at build time, at ERROR, with all
three declared versions printed and the blast radius named — *"The kit itself is unaffected
and keeps serving; only /contract.tar.gz and /contract/version 503."* The public `detail`
remains the surface's generic message. Consequence worth recording: an operator correlating
a burst of 503s now finds **one** ERROR line at build time, not one per caller.

**Full re-run matrix — 153 assertions, 0 failures**, 12 broken states. Both contract
representations 503 together in all eleven defect rows (previously `/contract/version`
served 200 in five of them, including `"2.0.0"` in the false-compatibility row); the
thin-`schema/` content-defect control still 200s on **both**; and the kit-surface columns —
`START.md`, `/version`, `kit.tar.gz`, `/kit/README.md`, `/kit/kit.json`, and the mount
root — assert 200 in **every** row, on top of both mounts for the contract paths.

**Three harness defects found and fixed before the results were trusted — two of them a
new vacuity shape worth naming.** (a) The kit-surface list included `/kit/kit.json` in the
case that *deletes* `kit.json`, where 404 is correct rather than a regression. (b) The
enlarged harness exceeded the 120/min limiter and began measuring **429s while reporting
them as failures of the guard**; fixed by installing a fresh `RateLimiter` per request,
mirroring the repo's own autouse fixture. (c) That same exhaustion made the memoisation
counter read **`0 evaluations`** — which would have read as a *pass* had the expectation
been 0 rather than 1.

Record (b) and (c) as the **inverse of the stale-snapshot trap**: not a green that tested
nothing, but a green that tested the *wrong thing* and would have confirmed the hypothesis
by accident. The general lesson for the standing checklist's spirit: an instrument that can
fail silently must itself be checked against a case whose expected answer is non-trivial —
a counter expected to read 0 cannot distinguish "correctly zero" from "measured nothing".

**Non-vacuity.** Same mechanism as the earlier backend rounds, stated as a mechanism:
in-process `TestClient` inside the running backend container with
`local_agent_kit_service.local_kit_dir` monkeypatched to a container-local copy of the
working tree; not the snapshot path, since `./backend/app:/app/app` is bind-mounted and
writing there would write into the repo. Side-by-side tree print opens every run; served
body carries `"contract_version": "1.0.0"`, a key the stale snapshot cannot produce. **Live
contrast, now consistent in both directions:** against the real pre-sync snapshot the
deployed instance 503s on *both* contract paths while `START.md`, `/version` and
`kit.tar.gz` keep serving — D16 amended working exactly as specified on the one instance
that actually has a broken contract today. Still Phase 11's to fix.

**Final state:** `tests/api/cli/` 142 passed; `mypy` clean on both files; `ruff` clean on
every added line (the remaining `UP012` and two `format --diff` hunks are pre-existing
lines the diff never touches — verified zero overlap).

### Bucket 13 — F6: `cmd_export` added a deprecated `cloud` block unconditionally

**Status: VERIFIED (F6 fix run).** Appended after Bucket 12; Buckets 1-12 untouched. This
bucket **corrects the record** of Buckets 7 and 9-11: the defect was live in the code those
phases reviewed and none of them caught it.

**The defect.** `cmd_export` cleared publication links in the exported copy like this:

```python
manifest["cloud"] = {"platform_url": None, "agent_id": None, "imported_at": None}
if "publications" in manifest:
    manifest["publications"] = []
```

The `cloud` assignment was **unconditional** while the `publications` clear beside it was
guarded. On a manifest with no `cloud` key, the assignment is not a clear — it is an
**insert**. Two consequences, both real:

1. **It inverts D11.** D11 deprecates `cloud` in favour of `publications[]`. Export was the
   one path that put the deprecated key *back* into manifests that had already moved on.
   The working tree's scaffold is exactly such a manifest: the template ships
   `publications: []` and no `cloud` at all, so *every* fresh agent's first export
   reacquired the key the scaffold had just shed.
2. **It broke key order.** Python dicts preserve insertion order, so the inserted key landed
   **after `publications`**, at the end. Phase 6 step 5 asserts insertion order preserved as
   part of the byte-identity contract with the desktop; an appended `cloud` is a byte
   divergence on a file whose byte shape is contractual.

**Why "verified clean" did not catch it — the honest account.** The line was **not in any
reviewed diff**. It is pre-existing code: at `HEAD`, `cmd_export` contains that one line and
no `publications` line at all, and the `HEAD` template manifest ends with a populated
`cloud` object and carries no `publications`. Under that premise the unconditional
assignment was **correct and total** — every manifest in existence had a `cloud` key, so the
assignment always rewrote in place and always preserved order. Three things then went wrong
at once, and each phase's scope hid the defect from that phase:

- The phase that **invalidated the premise** — replacing the template so `cloud` gives way
  to `publications[]` (Phase 6 step 2's key list) — edited
  `templates/agent/cinna-agent.json`, a **different file**. Nothing in that diff pointed at
  `cmd_export`.
- The phase that **edited the adjacent line** — Phase 5 bullet 6, adding the guarded
  `publications` clear — reviewed the line it added and not the line it stood next to. The
  guard was written correctly one line below the missing one.
- Bucket 7h **looked straight at this block** and reasoned about it, but asked a different
  question: *is the `cloud` → `publications[]` migration observable?* It answered that
  correctly ("unobservable by construction — the same bullet clears both"), and answering it
  retired the block from further attention. Nobody then asked the orthogonal question:
  *is clearing `cloud` safe on a manifest that does not have one?* 7h's sentence remains
  true as written; it is the question it did not ask that let F6 through.

The transferable lesson, in the shape of Bucket 10's HIGH 1 and Bucket 12's harness
findings: **a correct guard on a new line is evidence about that line only.** When a phase
adds a conditional beside an unconditional one doing the same job, the asymmetry is the
finding — either the new guard is unnecessary or the old line is missing one, and both
cannot be right. Here the asymmetry was visible in the final text of the diff and read as
normal. Related class: a claim can be invalidated by a phase that never touches its file,
so "not in this diff" is not "not affected by this change".

**The fix.** Guard the `cloud` clear on the key already being present, mirroring the
`publications` line, and extend the comment to record *why* the guard exists — that export
must never ADD a deprecated key a manifest did not have, and that insertion order is
contractual — so a later reader does not simplify the conditional away. Export now clears
what is there and never introduces a key the source lacked. Nothing else in `cmd_export`
changed.

**Evidence.**

- **Behaviour, both directions.** Same scaffold (`kit.py new`, manifest ends
  `… features, publications`, no `cloud`), exported by two kit copies differing **only** in
  that one guard. Pre-fix: exported manifest's last three keys are
  `['features', 'publications', 'cloud']`, `'cloud' in manifest` → `True`. Post-fix:
  `['handovers', 'features', 'publications']`, → `False`. The complementary case is the
  non-trivial one and was checked rather than assumed: a manifest carrying a **populated**
  `cloud` between `runtime` and `status_refresh_command` still exports with `cloud` present,
  **at its original position**, zeroed to
  `{'platform_url': None, 'agent_id': None, 'imported_at': None}` — the guard fixes the
  insert without weakening the clear.
- **Non-vacuity, bidirectional.** `docker compose cp` of both kit copies to
  `/tmp/kit_f6_before` and `/tmp/kit_f6_after`, run under `LOCAL_AGENT_KIT_DIR`. Probed
  inside the container in both directions: new-guard count 0 / old-unconditional count 1 in
  the `before` copy, 1 / 0 in the `after` copy — the old anchored form reads 0 post-fix
  precisely because the surviving assignment is now indented under the guard. Strongest
  form: `md5` of the container's `after` copy is **byte-identical** to the working-tree
  `kit.py` (`960db663…`) and differs from the `before` copy (`05091db2…`).
- **Instrument failure caught in this run — a fifth, as predicted.** The first
  before/after export pair produced *identical* output and looked like "no behaviour
  change". Both runs had in fact hit `main()`'s Python-3.10 version gate under the macOS
  system `python3` (3.9), printed the uv install advice to stderr and returned 1 without
  executing `cmd_export` at all. It reads as a clean negative result. Re-run under
  `backend/.venv/bin/python` (3.13.5), the difference appeared immediately. Same family as
  Bucket 12 (b)/(c): an instrument that fails silently produces a *plausible* answer, and
  here the plausible answer was the wrong conclusion. **Later correction — the ordinal only.**
  The finding stands; "a fifth" does not, and no replacement number is derived here because
  quoting one forward is the defect. See the standing checklist's rule that a figure is true
  only as of its own bucket.
- **Suite.** `tests/unit/test_local_kit_tool.py`, container mode with
  `LOCAL_AGENT_KIT_DIR`: **9 failed, 30 passed** before the fix and **9 failed, 30 passed**
  after, the same nine names. F6 neither fixes nor breaks any of them — they are Phase 6/7
  work that has not landed. Standing list for the next agent:
  `test_new_scaffolds_and_validate_exits_zero`,
  `test_validate_json_report_is_machine_readable`,
  `test_validate_warnings_are_not_vacuous`,
  `test_validate_fix_regenerates_workspace_requirements`,
  `test_export_excludes_local_only_paths_and_clears_the_cloud_block`,
  `test_template_manifest_matches_the_shipped_schema`,
  `test_cloud_ready_promotes_readiness_advice_to_errors`,
  `test_export_applies_the_cloud_ready_gate`,
  `test_only_the_declared_prompts_are_required`. The ones inspected fail on the same root
  cause — `validate` still errors ``cinna-agent.json: `schema_version` must be an integer``
  against a template that no longer ships `schema_version`, which is Phase 7's contract
  gate. Worth noting that
  `test_export_excludes_local_only_paths_and_clears_the_cloud_block` — the one test whose
  name sounds like F6's — **would not have caught it either**: it explicitly *writes* a
  populated `cloud` into the manifest before exporting, so it only ever exercises the
  cloud-present branch. **Phase 12 owes a case for the cloud-absent branch**, asserting both
  that `cloud` is absent from the export and that the exported key order is unchanged.

### Bucket 14 — Phase 7 findings (`kit.py validate`: contract gate, new fields, severity parity)

**Status: VERIFIED (Phase 7 implementation run).** Appended by Phase 7; Buckets 1–13
untouched. One code file edited: `docs/local_agent_kit/tools/kit.py`. No test file, no
write path, and no `cloud` → `publications[]` migration-on-write was touched.

**14a. The severity table as implemented.** Every row of Bucket 3 landed. Checked by
running `validate` over a scaffold mutated one condition at a time and reading the channel
the finding came back on, not by reading the diff.

| Row | Landed | Where it now lives |
|---|---|---|
| K1 pyproject ⇄ `workspace_requirements.txt` | warning (both branches: file missing, deps missing) | `_validate_requirements` |
| K2 description is the scaffold placeholder | warning; `--cloud-ready` promotion kept | `_validate_cloud_readiness` |
| K3 `EXPECTED_FILES` missing | warning (unchanged) | `_validate_files` |
| K4 missing `.gitignore` | **error → warning** | `_validate_files` (`REQUIRED_FILES` loop now branches: the manifest stays an error, the ignore file does not) |
| K5 workflow prompt empty | **error → warning** | `_validate_files` |
| K6 workflow prompt still holds a placeholder | **error → warning** | `_validate_files` |
| K7 `credentials/.env` tracked by git | **error → warning** | `_validate_secrets` |
| K8 git unavailable | info (unchanged) | `_validate_secrets` |
| K9 key-material filename | warning (unchanged) | `_validate_secrets` |
| K10 forbidden value key in a credential slot | **kept an error**, with the reasoning written into the code so it is not "aligned away" | `_validate_credentials` |
| K11 schedules but no `status` command | warning (unchanged) | `_validate_commands` |
| K12 `kit_version` predates the kit | info (unchanged) | `_validate_cloud_readiness` |
| K13 the `--cloud-ready` promotion itself | documented in code as kit-only, with the reason it is not a divergence | `_validate_cloud_readiness` |
| S1 duplicate credential slot name | **error → warning** | `_validate_credentials` |
| S2 unknown credential `type` | **error → warning**; `type` missing/empty is now a separate **error** (`type_missing`), which the old single check conflated | `_validate_credentials` |
| S3 handover target folder missing | **error → warning** | `_validate_cloud_readiness` |
| S4 catalogued command with no Makefile target | warning (already aligned) | `_validate_commands` |
| S5 `example_prompts` empty | warning outside the gate (already aligned) | `_validate_cloud_readiness` |
| P1 command-name pattern | reconciled: `COMMAND_NAME_RE` is now the desktop's `^[A-Za-z0-9][A-Za-z0-9_-]*$` and is the **error**; the kit's stricter lowercase/≤32 rule became `COMMAND_NAME_CONVENTION_RE` and is a **warning** | `_validate_commands` |
| D-1 `/run:` reference unresolved | added, **error** | `_validate_commands` |
| D-2 unparseable catalog entry | added, **error** | `cli_command_entries` + `_validate_commands` |
| D-3 `example_prompts` bounds (>20, item >500) | added, error | `validate_manifest` |
| D-4 self-handover | added, warning | `_validate_handovers` |
| D-5 unroutable (no trigger, no examples) | added, warning | `validate_manifest` |
| D-6 STATUS.md frontmatter / `status` field | added, warning | `_validate_status_file` + `_parse_frontmatter` |
| D-7 non-boolean `features` member | added, warning (the "`features` is not an object" error is kept alongside it — Bucket 3e's two-severities-one-code shape, deliberately mirrored) | `validate_manifest` |
| D-8 `scripts/` with no `scripts/README.md` | added, warning — this was the early return, so the *worst* case was the one case reporting nothing while every partially-catalogued folder was warned about | `_validate_scripts_catalog` |
| D-9 deprecated `cloud` block | added, **info**, read-only — `validate` does not migrate it | `_validate_publications` |

**14b. S2 lands, checked rather than assumed (Bucket 10's consistency ask).** A slot with
`"type": "brand_new_type"` now comes back on the warnings channel with exit 0. The shipped
schema's D13 wording — that an unrecognised type is tolerated and reported as a warning — is
therefore a true statement about the bundled artefact, not an aspiration. The evidence is a
run, not a diff: the mutated manifest was validated and the finding read off the `warnings`
array of `--json`.

**14c. `template_description()` was already correct — Phase 6 did fix it.** It returns
`description.replace(scaffold_token("DESCRIPTION"), DEFAULT_DESCRIPTION)`, i.e. it compares
against the *substituted* default and not the raw token, and it carries a comment saying
why. K2 fires on a fresh scaffold, verified by run. Nothing was owed here.

**14d. Divergences from the desktop this phase decided, each deliberate.**

1. **An unusable *tool* contract version is info, not an error.** The desktop's
   `checkIdentity` routes `checkContractCompatibility`'s `unknown` status to
   `manifest.contract_version.invalid`, an **error**, whichever side was unparseable — so a
   host that cannot read its own bundled contract version blames the agent folder for it.
   `kit.py` splits them: an unparseable **folder** version is an error, an unparseable
   **kit** version is an info that says the gate did not run. Worth telling them: it is a
   one-line fix on their side and it currently mislabels a host problem as a folder defect.
2. **`kit.py` findings carry no machine-readable code.** `Report` has four channels
   (`error`/`warn`/`info`/`fix`) and each finding is a bare sentence; the desktop's `Finding`
   carries `{code, message, path}`. So a conformance harness keyed on `code → severity`
   **cannot** be run against `kit.py` as it stands — it can only diff severities by matching
   message text. Adding a code channel is a structural change to the `--json` payload and
   was out of Phase 7's scope; flagged here because Bucket 3e already warns such a harness
   will not round-trip on the desktop side either, and this is the other half of that
   problem. If a harness is actually wanted, it needs a phase.
3. **D-2 is implemented as "an entry no name can be taken from", not as a full mini-YAML
   parity port.** `kit.py` scans lines; the desktop runs a mini-YAML reader and reports the
   issues it flags. The kit now reports a `commands:` entry it cannot name and an entry whose
   `name:` reads as empty — the two cases that previously vanished silently and took the
   Makefile-mirror check, the duplicate check and the `/run:` resolution blind with them.
   The reader was also widened to accept a `name:` that sits **below** `description:` inside
   an entry, which a real YAML reader sees and the old dash-line-only scan did not: without
   that, D-2 would have refused catalogs the desktop runs, i.e. diverged in the worse
   direction while claiming to close a parity gap.
4. **`contract.older` (their `checkFiles` step 7) was not ported.** It is an info fired
   whenever the folder's contract sorts below the kit's on any component, and it duplicates
   the `migratable` warning for the case that matters. Not in Phase 7's addition list; noted
   so a later reader knows it was a decision and not an oversight.
5. **`_validate_commands` no longer returns early when the command catalog is absent.**
   The desktop's `readCommandCatalog` returns an empty catalog for a missing file and then
   still resolves `/run:`, so a manifest naming `/run:status` with no catalog at all is an
   error there and used to be silent here. Now matched. This is a real behaviour change for
   folders that deleted the catalog but kept `status_refresh_command`.

**14e. The `--json` payload gained both sides of the gate.** `contract_version` (read from
the folder's manifest, `None` when absent or unreadable) and `tool_contract_version` (from
`kit.json`, falling back to the `CONTRACT_VERSION` file — **never** the network). A harness
can now see what the verdict was computed from instead of inferring it from a sentence.

**14f. `schema_version` is now inert, exactly as D11 requires.** Nothing branches on its
value anywhere in `kit.py`; `SUPPORTED_SCHEMA_VERSION` is deleted. Its only remaining role
is presence: `schema_version` present with **neither** `contract_version` nor `id` selects
the legacy branch, which warns and skips the rest of the identity checks — verified to emit
zero errors, which is the point of the branch. `schema_version` alongside `contract_version`
emits the "legacy and ignored" info.

**14g. CONTRADICTS A LANDED PHASE — Bucket 6b's claim about
`test_template_manifest_matches_the_shipped_schema` is wrong, and the test cannot pass as
written.** Bucket 6b states the test "does not break and does not need Phase 6 to stay
green", verified "empirically against a 12-case behaviour matrix". The template Phase 6
actually landed carries unsubstituted scaffold tokens:

```
"contract_version": "{{CONTRACT_VERSION}}",  "id": "{{ID}}",  "slug": "{{SLUG}}"
```

The test validates *that file* against the shipped schema. `jsonschema` rejects it on
`'{{SLUG}}' does not match '^[a-z0-9][a-z0-9-]{1,62}$'` — a `slug` failure that has nothing
to do with the identity `allOf` the matrix was exercising, and that fails before any
identity branch is reached. The stdlib half fails identically and for the same reason (plus
the `id`/`contract_version` patterns). **This is not Phase 7's to fix and was not fixed:**
loosening the schema patterns to admit token strings would make the schema stop rejecting a
folder whose scaffold never ran, and `validate` correctly refuses such a folder today.
Either the test must validate a *scaffolded* manifest rather than the template (Phase 12),
or the template must ship substitutable-but-valid placeholder values (Phase 6/10). It is a
decision, not a bug fix, so it is recorded rather than taken. The transferable lesson is
Bucket 5f's shape one layer up: **the matrix verified a hypothetical Phase 6 template, not
the one that landed** — a check run against a stand-in for the artefact, reported as a check
of the artefact.

**14h. Test outcome, stated as numbers.** Baseline 9 failed / 30 passed; after Phase 7,
**4 failed / 35 passed**. **Six** of the nine closed — not the seven the phase brief
expected — and one currently-passing test flipped to failing as a *direct and intended*
consequence of a mandated demotion. All four survivors are stale test expectations owned by
Phase 12; none is a code defect, and no test was edited. The seventh the brief expected was
`test_template_manifest_matches_the_shipped_schema`, which the brief's own root-cause
diagnosis (`schema_version` errors from the old gate) covered only half of: removing the
gate fixes the stdlib half and leaves the `jsonschema` half failing on `{{SLUG}}` — see 14g.
The six closed are `test_validate_json_report_is_machine_readable`,
`test_validate_warnings_are_not_vacuous`,
`test_export_excludes_local_only_paths_and_clears_the_cloud_block`,
`test_cloud_ready_promotes_readiness_advice_to_errors`,
`test_export_applies_the_cloud_ready_gate` and
`test_only_the_declared_prompts_are_required`.

- `test_new_scaffolds_and_validate_exits_zero` — dies on `manifest["schema_version"] == 1`
  with a `KeyError` before it ever runs `validate`. Phase 6 removed the key from the
  template. The half of it that Phase 7 owns does pass, proven by
  `test_validate_json_report_is_machine_readable` (same fixture, same command, now green).
- `test_validate_fix_regenerates_workspace_requirements` — asserts `returncode == 1` for the
  K1 finding, which Bucket 3/K1 mandates be a warning. `--fix` still repairs the file; only
  the exit code changed.
- `test_validate_fails_on_empty_workflow_prompt` — the flip. Asserts `returncode == 1` for
  K5, which Bucket 3/K5 mandates be a warning (the desktop's `files.prompt_empty` is one).
  The finding still fires, on the warnings channel.
- `test_template_manifest_matches_the_shipped_schema` — 14g. Fails in `jsonschema` before
  `kit.py` is consulted at all.

**14i. Non-vacuity evidence for this phase's run.** Mode: `docker compose cp
docs/local_agent_kit backend:/tmp/kit_p7` then `docker compose exec -T -e
LOCAL_AGENT_KIT_DIR=/tmp/kit_p7 backend python -m pytest tests/unit/test_local_kit_tool.py`,
re-copied after the last edit. Bidirectional, by md5 rather than grep, since this was a
replacement edit: host `md5 -q docs/local_agent_kit/tools/kit.py` and container
`md5sum /tmp/kit_p7/tools/kit.py` both `e5cbc265a4eeb4cd8ba46930caaa73db`, which settles
presence *and* absence in one number — the baseline hash of the same file was
`960db663aed26ed6227127ac8ebe87d9`. Belt and braces: inside the container copy,
`grep -c SUPPORTED_SCHEMA_VERSION` = **0** and `grep -c check_contract_compatibility` = **2**.
`tests/api/cli/test_local_agent_kit.py` and `tests/api/server_config/test_server_config.py`
were run too (91 passed) but are **not** evidence about this edit: they exercise the service
against the image's own snapshot, which no `LOCAL_AGENT_KIT_DIR` reaches.

**14j. F3 check.** `grep -nE '\{\{[A-Z][A-Z0-9_]*\}\}' docs/local_agent_kit/tools/kit.py`
returns nothing — no platform-render-shaped token literal was introduced. The five surviving
`{{` occurrences are pre-existing (`scaffold_token`'s docstring and body, two f-string-escaped
slug patterns, the `base_url` unrendered-token guard) plus the K6 message, whose
`` `{{{{...}}}}` `` renders as a lowercase-dotted placeholder and cannot be substituted.
Whoever writes about tokens in this file next should build them with `scaffold_token()`, as
that helper's docstring already explains.


### Bucket 15 — the export-mutation investigation, and the R4 ruling

**Status: VERIFIED (read-only investigation run).** Appended after Bucket 14; Buckets 1–14
otherwise untouched, with one exception ruled separately by the team lead: Bucket 6's status
line and 6b carry an in-place correction, appended rather than substituted, and 15f records
why. Nothing in this bucket was established by reading a diff; every claim below carries the
file and line it was read from, and those citations are the evidence rather than decoration.

**15a. The hypothesis this investigation was dispatched on is FALSE, and is recorded dead
rather than quietly dropped.** The worry was that `cmd_export` rewrites `cinna-agent.json`
in the exported copy while the desktop uploads it verbatim, so the two hosts would compute
`content_hash` over different bytes and disagree permanently. It does not happen. `cmd_export`
computes the digest from **`source`**, from one walk of the source tree, at
`docs/local_agent_kit/tools/kit.py:2497-2498` — *before* `destination.mkdir` (`:2511`),
before any file is copied, and 58 lines before the manifest rewrite (`:2539` reads it,
`:2556` writes it). Phase 5 step 3 anticipated exactly this trap and the code carries a
comment saying so in as many words: *"The tree that travels and the hash every host computes
over it come from ONE walk of the SOURCE — never from the destination, which this command
then edits."* The desktop likewise hashes in place (`hashExportFiles`, below). **No divergence
ever existed through that door.** A dispatched hypothesis that dies has to be written down as
dead, or the next reader spends the same afternoon re-deriving it.

**15b. The desktop has no export copy, and no publish path at all.**
`/Users/evgenyl/dev/ml-llm/cinna-desktop/src/main/kit/exportTree.ts` is a **view** over the
folder in place — file list, hash, byte count — not a copier. From `node:fs` it imports
`readdirSync`, `readFileSync` and `statSync` and nothing that writes (`:14`); there is no
destination parameter, no write call and no `mkdir` anywhere in the module. Its three
exported functions — `collectExportFiles:50`, `hashExportFiles:93`, `buildExportTree:116` —
each take a single `agentDir`.

It has **zero production callers.** The only `import` of it anywhere in their tree is its own
test file (`src/main/kit/exportTree.test.ts:12`); the one other mention is a prose comment in
`src/main/kit/hash.ts:4`. Nothing in the desktop **writes** `publications[]` or `cloud`
either — the complete set of references is a type declaration
(`src/shared/kit/manifest.ts:77` for `AgentPublication`, `:122` for the manifest property),
read-only validator checks (`src/main/kit/validator.ts:504-545`, `checkPublications`), one
scanner read (`src/main/services/localAgents/scannerService.ts:419`) and one renderer card
(`src/renderer/src/components/agents/local/ReadOnlyCards.tsx:233-246`). Their own tech doc
states the publish consumer in the future tense — *"Publish must refuse while `unreadable` is
non-empty"*
(`/Users/evgenyl/dev/ml-llm/cinna-desktop/docs/agents/local_agents/kit_contract_tech.md:170`
— their repo; absolute so it is not read as a cinna-core `docs/` path).

**This sharpens the tree-upload gap already ranked first in §0; it is not a second finding.**
The primitive exists, is well built, is tested, and has nothing to call it — because the
route it would push to does not exist on our side. Cross-reference the ranked "tree-upload
gap" section rather than restating its argument here or there; the answer-back must not read
as though these were two separate obstacles.

**15c. The defect that forced the R4 ruling.** Both hosts hash `cinna-agent.json` — neither
exclude list contains it, and their own test pins it into the travelling set as the first
entry of the expected file list (`src/main/kit/exportTree.test.ts:70`, inside the
`expect(files).toEqual([...])` assertion at `:69-78`). So
`publications[].content_hash` was a value stored **inside the file it is a hash of**.
Reaching that fixed point requires producing a SHA-256 preimage on demand: not difficult,
impossible. Concretely: the first publish computes h₀ and writes it in, the manifest bytes
change, the next scan computes h₁ ≠ h₀, and the folder reads *"1 unpublished change"* the
instant the publish **succeeds**. Republishing to clear it creates the next mismatch;
publishing to instance B makes instance A read "behind". It is D7's "unpublished changes
forever" reached by a third route, and unfalsifiable from the user's side — a number that
never reaches zero, with nothing on screen explaining why.

**Nobody would have decided this.** It is not a design anyone argued for and lost; it is
simply what you get by writing the field into the file you already hash. Worth saying in the
answer-back in exactly that register, because it is the difference between reporting a defect
and assigning a fault.

**15d. The ruling: R4.** The team lead ruled that `publications[]` leaves the manifest and
becomes a sibling `publications.json` at the agent root, named root-anchored as a plain
string in `cloud_import_excludes`. **D11 is amended, not superseded**, and the amendment is
already written into `docs/plans/local_agent_kit_desktop_contract_requirements.md` under D11
— including the four implementation constraints, the "correction not compromise" reasoning
and the "why now". Cross-reference it; do not restate it in full here or in the answer-back.

**Why R2 was rejected — record this, because it is the part a future reader will
re-litigate.** R2 was to keep the keys in the manifest and declare a strip-rule in
`layout.json`, hashing over a canonicalised copy with those keys removed. It is coherent, and
would have been taken if its fail-safe held. It does not:

- Their `parseLayout` (`src/main/kit/layout.ts:183-231`) builds its result **field by field
  from a known key list** — `contract_version`, `workshop`, `agent`, `scaffold_ignore_files`,
  `desktop_owned`, `cloud_import_excludes`, `local_command_runner` — and **silently ignores
  any top-level block it has never heard of.** No throw, no `logger.warn`, no
  degraded-contract path. (The one `logger.warn` in it fires only for an empty or non-object
  document.)
- Their version gate passes any same-major pair, pinned by their own test:
  `checkContractCompatibility('1.0.0', '1.4.2').status` is `'ok'`
  (`src/main/kit/contractVersion.test.ts:52`, against
  `src/shared/kit/contractVersion.ts:92`).

So a strip rule shipped at 1.1.0 reaches an existing desktop as **silence**: rule ignored, no
warning, hash computed the old way, folder reported healthy, number never zero. That is the
D15 fuse one level up — the only channel that reaches a non-adopting reader loudly is a
**major** bump, on a contract being shipped at 1.0.0. R2 also contracts a permanent
cross-language canonical-serialisation obligation, and Python and JavaScript already disagree
on `1e-07` vs `1e-7` and on integers past 2⁵³. R4 dissolves the problem instead of paying
that liability forever to manage it.

**One implementation trap, worth a line in the answer-back because it fails open on the one
list that gates secrets:** do **not** smuggle a rule into `cloud_import_excludes` as an
object entry. That list goes through `asStringArray` (`src/main/kit/layout.ts:228` for
`desktop_owned`, `:229` for `cloud_import_excludes`), a `typeof v === 'string'` filter — an
object entry is dropped without a sound, and what it silently shortens is the exclude list.
`publications.json` goes in as a plain string, root-anchored, per D11's amendment constraint 3.

**15e. Three items that stand regardless of the ruling. These are answer-back material.**

1. **The strip-free payload divergence — state it as what the desktop must not do, not as an
   asymmetry between us.** Our export strips publication links from the copy; their publish,
   when it is written, has no stripping code anywhere in the tree. If they upload the file
   list `collectExportFiles` names — the obvious implementation, since it is their only
   export primitive — the desktop pushes **another instance's `platform_url` and `agent_id`**
   into the cloud copy. That is a **payload** divergence, not a hash divergence, and it
   survives R4 on their side. R4 removes the stripping from our side too, because the file no
   longer travels at all; so this is not "we strip and you don't", it is "after R4 there is
   nothing to strip, and a publish that uploads `publications.json` reintroduces the problem".

2. **The two bundled `layout.json` files differ while both declare
   `"contract_version": "1.0.0"`.** Theirs
   (`/Users/evgenyl/dev/ml-llm/cinna-desktop/resources/cinna-kit-contract/layout.json`)
   carries 34 exclude patterns to our 40. It lacks `temp/`, `credentials/`, `**/*.env`,
   `__pycache__/`, `.mypy_cache/` and `.ruff_cache/` (both the root and `**/` forms of the
   last three), lacks the entire `secret_files` block, and types `desktop_owned` as
   `string[]` (`["app-data/desktop.json"]`) where ours is `object[]`. Same declared version,
   different content — the version number cannot distinguish them, which is the whole reason
   this needs saying out loud. **The consequence is the sharp part: every hash-parity claim
   in §0 is a claim about a post-adoption desktop, and none of them holds against the build
   on their disk today.** This is folded into the "Required desktop-side changes" list by
   **sharpening the existing D5 re-bundle entry** — the re-bundle is
   `resources/cinna-kit-contract/` wholesale, `layout.json` included — rather than added as a
   duplicate bullet.

3. **Phase 5's "assert `content_hash` in a test" is unmet, and this is the item most likely
   to be trimmed.** `grep -n content_hash backend/tests/unit/test_local_kit_tool.py` returns
   nothing: there is no `content_hash` test at all. The source-before-rewrite ordering that
   15a just verified — the single property that makes our hash agree with theirs — is
   therefore **uncommitted and unpinned**, holding only because the current code happens to
   be written that way. A later reader tidying `cmd_export` can move the digest below the
   copy and nothing will object. **Flag it against Phase 12** as a required case, not an
   optional one: assert that the digest is computed from the source, and that reordering it
   changes nothing observable only because it is asserted.

**15f. The seventh instrument failure of this run, and it is a NEW SHAPE — this is why it
gets its own entry instead of joining the existing ones.** The failure is Bucket 6b: a
12-case behaviour matrix, correctly constructed and correctly run, that verified a
**hypothetical Phase 6 template** rather than the one that landed, and reported the result as
a fact about the artefact. See the correction appended to Bucket 6b, and Bucket 14g, which
found the same thing from the other side.

**Later correction — the ordinal only.** "The seventh" is unreliable and no replacement is
derived here, per the standing checklist's rule that a figure is true only as of its own
bucket; the finding and its NEW SHAPE claim are untouched. It may also **double-count Bucket
14g**, which is this same defect found from the other side rather than a separate instrument
failure.

The distinction is the transferable part, and it must not be blurred into the earlier
entries:

- The earlier instrument failures were a **broken tool** or a **vacuous green** — the
  stale-snapshot test that saw none of the working tree (Bucket 5f), the rate-limited harness
  that measured 429s and reported them as guard failures, the memoisation counter reading
  `0 evaluations` because it had measured nothing (Bucket 12 (b)/(c)), the Python-3.9 version
  gate that returned 1 without running `cmd_export` and looked like "no behaviour change"
  (Bucket 13).
- This one is **neither**. The instrument worked. The measurement was sound. It was taken of
  the **wrong artefact**.

**And the mitigations differ, which is the reason the shapes must stay separate.** The
earlier family is caught by probing the artefact — md5 the container copy against the working
tree, run a case whose expected answer is non-trivial, check the tool actually executed. None
of that catches this one: every such probe would have come back clean, because the matrix
*was* correctly probing something. This one is caught only by a different question —
**is the artefact I probed the artefact that shipped?** A green from a sound instrument is
evidence about the input it was given and about nothing else.

**15g. The template is not a valid manifest, by construction, on both sides — tell them
before they discover it.** Any test either side writes asserting that the shipped template
validates against the shipped schema **will fail**. Ours ships `"slug": "{{SLUG}}"` and five
other unsubstituted tokens (`docs/local_agent_kit/templates/agent/cinna-agent.json:2-8`);
theirs ships the same shape (`resources/cinna-kit-contract/templates/agent/cinna-agent.json:2-8`),
and their `MANIFEST_TOKENS` (`src/shared/kit/manifest.ts:128-136` — `SLUG`, `NAME`,
`DESCRIPTION`, `ID`, `CONTRACT_VERSION`, `KIT_VERSION`, `CREATED_AT`) guarantees it stays
that way. The schema's own `SLUG_PATTERN` (`src/shared/kit/manifest.ts:141`,
`^[a-z0-9][a-z0-9-]{1,62}$`) rejects `{{SLUG}}` before any identity branch is reached.

**The resolution, and the one option that must not be taken.** The test must validate a
**scaffolded** manifest, never the raw template. Loosening the schema patterns to admit token
strings would make the schema stop rejecting a folder whose scaffold never ran — which is
precisely the case it exists to catch, and `validate` correctly refuses such a folder today.
Better they read this from us than spend the afternoon we spent.

**15h. Finding codes: DEFERRED, and the answer-back must put it as a joint proposal rather
than an observation about them.** `kit.py`'s `Report` has four channels
(`error`/`warn`/`info`/`fix`) and each finding is a bare sentence; their `Finding` carries
`{code, message, path}`. Ruling: **defer.** Adding codes to `kit.py` unilaterally buys
nothing until they change too, and the change is additive and non-breaking whenever it
happens, so there is no cost to waiting and no cheaper moment being missed.

Record it as a concrete follow-up with a named target, not as a grievance: **their
`{code, message, path}` shape is the sensible one** and is what both sides should converge on;
both sides need it before §9.2 parity can be mechanical; and Bucket 3e already records that a
`code → severity` harness will not round-trip on **their** side either, so this is a shared
gap and not a debt one side owes the other.

**State this last point plainly and prominently, because §9.2 currently reads as though it
were automatable today and it is not: until both sides carry codes, the §9.2 conformance
check is a MANUAL comparison** — severities matched by reading message text against message
text. Someone will otherwise plan around a harness that cannot exist yet, and discover it at
the point where they were counting on it.

**15i. The fail-safe pair, as a transferable lesson — §0 must carry it, because §0 is what
the answer-back is written from.** The full statement lives in the requirements document's
D15 (with D11's amendment adopting the second half); cross-reference it rather than
duplicating the derivation. The lesson itself:

- For `secret_files`: **unevaluable ⇒ treat as secret, withhold the file.**
- For anything affecting a hash: **unevaluable ⇒ refuse to emit `content_hash` at all** —
  never emit one computed a different way.

The two directions are **opposite**, and they are opposite because the consequences are: a
leaked secret is unrecoverable, while a plausible wrong drift number is untraceable and a
missing one is merely visible. So **the safe direction is a property of the consequence, not
a house style.** A host that copies the direction instead of deriving it will get the next
case backwards — which is the whole reason this is written as a pair rather than as two
rules, and the reason it belongs in the answer-back at all.


### Bucket 16 — R4 implementation findings (`publications.json`)

**Status: VERIFIED (R4 implementation run).** Appended after Bucket 15; Buckets 1–15
untouched except for the two stale-figure corrections marked in place in 9f and Bucket 10,
which point here.

**16a. `cmd_export` no longer writes the manifest at all, and the existence assertion beside
it survives — the two were explicitly distinguished rather than removed together.** Byte
parity was measured, not reasoned about: on a source manifest carrying **both** a populated
`cloud` and a populated `publications[]`, the pre-R4 export produced a manifest that differed
from the source (`12d423b5…` against `f05b20e7…`, two hunks); after R4 `diff` reports the
exported manifest **byte-identical** to the source.

The kept `if not manifest_path.is_file()` check (`tools/kit.py:2767-2770`) was proven still
load-bearing by a **data-only mutation**: a kit copy whose `kit.py` md5 equals the working
tree's, with only `layout.json` gaining `cinna-agent.json` in `cloud_import_excludes`, exits 1
with `cinna-agent.json was not copied — check layout.json cloud_import_excludes`. That
separation is the point of recording it. §0's shapes section already catalogues this contract's
history of a filter standing where an assertion belonged; the temptation when deleting a
rewrite is to delete the
check that sat next to it, and the check is the half that is not about rewriting. The comment
above it now says so, so a later reader does not tidy it away with the rewrite it outlived.

**16b. There is now exactly ONE manifest writer, which makes the ledger migration structural
rather than remembered.** Enumerated rather than assumed, and re-derived here rather than
quoted: `grep -n 'write_text(' docs/local_agent_kit/tools/kit.py` returns five sites, three of
which write other files (`workspace_requirements.txt` at `:1361`, the scaffold token
substitution at `:1551`, the refresh-check stamp at `:2248`); the remaining two are the
manifest (`:512`) and the ledger (`:516`), **both inside `write_manifest`**. Before R4 the
manifest was written from two places — `cmd_new`'s scaffold write and `cmd_export`'s rewrite. Export now
writes nothing, and `cmd_new` routes through `write_manifest` (`:1638`) with a comment saying
why, so `write_manifest` (`:426`) is the sole writer and **a write path added later cannot
forget the migration** — it gets it by calling the only function that writes the file.

Merge rule, as implemented: entries are keyed by `platform_url`; `publications[]` is absorbed
first so a `cloud` stamp naming the same instance loses to it; an entry already in the ledger
is never overwritten, because the file is the newer authority. The manifest is written from a
copy (`document = dict(manifest)`), so the caller's dict is not silently emptied.

**16c. The property R4 exists to buy, measured as a number rather than argued.** Across a
migration the folder's `content_hash` moves **exactly once** and is then stable: `h0 ≠ h1`,
`h1 == h2`. That is the whole ruling in three digests — the folder genuinely changed once, and
the perpetual drift produced by storing a tree's hash inside a file that tree contains is
gone. Two adjacent properties were verified in the same run: the ledger is **untouched when
nothing is absorbed**, so a hand-edited `publications.json` is never reformatted by a manifest
write that had nothing to migrate; and a `cloud` or `publications` value **this build cannot
read** is left in place rather than discarded, the fail-safe direction derived from the
consequence per 15i (discarding data a host cannot interpret is unrecoverable; leaving a
deprecated key is merely visible, and `validate` names it on every run).

**16d. Two defects the implementer found in its OWN first cut, both the Bucket 10 HIGH 1
shape: a safeguard applied at the level being thought about and not at the level above it.**

1. **`read_publications` conflated "no ledger" with "a ledger I cannot parse"**, returning
   `[]` for both. `write_manifest` then read a corrupt ledger as empty and **overwrote it** —
   silent, unrecoverable data loss, in the function whose entire job is moving data safely.
   Fixed with a tristate return (`[]` / entries / `None`, `:374-403`); `write_manifest` now
   raises `KitError` on `None` and writes **neither** file, because a half-migration that
   reported success is the failure shape the ruling exists to remove.
2. **`document.pop("publications")` was unguarded** while the `cloud` removal on the next
   lines was type-guarded — **literally the guard-asymmetry defect**, in code written after
   that heuristic had already been added to this project's review checklist
   (`.claude/commands/cinna-core.code.review.md:102-107`, itself an uncommitted change from
   this same run, added in response to F6/Bucket 13). Both removals are now guarded on
   `isinstance`, and both leave an unreadable value in place.

Worth recording plainly, because it is the most instructive single fact of the run: the
pattern recurred **immediately, in new code, written by someone who had been briefed on it**.
See the answer-back shapes section, which now carries the honest reading of what a checklist
entry like that does and does not buy.

**16e. `publications.json`'s top level is an object, not a bare array** — `{"publications":
[…]}`. The reasoning, since this is the kind of choice a later reader re-litigates: a bare
array has nowhere to put a ledger-format version or any sibling block, so the first such need
is a breaking change for every reader that already parses the file. An object absorbs
additions additively, which is the forward-compatibility contract `cinna-agent.json`,
`layout.json` and `kit.json` already state, and it keeps **every** contract JSON file
object-rooted, so the `isinstance(document, dict)` shape every `read_json` consumer assumes
still holds without a per-file exception.

The `publications` property was moved into `schema/publications.schema.json` and deep-compared
against the block extracted from the old manifest schema: **verbatim**, including
`content_hash`'s corrected post-D15 description ("over the files that survive both
cloud_import_excludes and the secret_files rules in layout.json"). Verified now from the tree:
`cinna-agent.schema.json` no longer carries a `publications` property, retains `cloud` with a
description saying where the ledger went and that it is *never* migrated at export time, and
`publications.schema.json` carries the corrected wording. **Caveat on the verbatim claim, so
it is not read as re-derived here:** the comparison base is not in git — `HEAD`'s manifest
schema has no `publications` block at all, since that block is itself uncommitted Phase 2
work — so the verbatim finding rests on the implementation run's own comparison, and only the
end state was re-checked.

**16f. Severity reasoning for the two new checks, argued in the code rather than picked.**

- **"The manifest still carries `publications`" is a WARNING** (`kit.py:1123-1142`). It cannot
  be an **error**: no other host's validator has this check, so it is kit-only, and Bucket
  3a/§9.2 forbid a kit-only error — a folder this kit calls broken that the desktop runs
  happily is the parity failure in the worse direction. It is not an **info** like the
  deprecated `cloud` block either, and the distinction is the substantive part: `cloud` is
  *retained* — still described by the schema, still read correctly, the only ask being a
  re-stamp — while `publications` here is *removed*. No host reads it from this file, and a
  host that writes a `content_hash` into it reproduces the unreachable fixed point 15c
  describes. Filing a removed-and-harmful key at the same level as a retained-and-inert one
  would be the wrong signal.
- **The `publications.json` entry checks stay ERRORS** (`kit.py:1145-1194`), because the
  desktop's validator checks the same fields at error severity — `validator.ts:512-540`, codes
  `manifest.publications.type` / `.item` / `.required` / `.field_type`. R4 moved **where** the
  data lives; it did not make checking it a kit-only concern, so the severity travels with the
  data. One deliberate exception: an object with no `publications` key at all is a warning, not
  an error — a file that says nothing rather than one that says something wrong.

**16g. Two judgement additions to `layout.json`, approved by the coordinator, and the
verification asked for.** An `agent.publications` pointer, so a host reads the ledger's
filename from contract data instead of hard-coding it in two places (the D15/D6 principle
applied one file over), and an `agent.roles` entry with `role: publication_ledger` whose
description carries the reason the ledger is a sibling of the manifest rather than a key
inside it.

**The claim that both are inert against their parser was checked against their source, and it
checks out — but the stated reason was incomplete, so record the fuller one.**

- `parseLayout` (`layout.ts:184-233`) does build `agent` field by field from a known key list —
  `manifest`, `prompt_files`, `command_catalog`, `status_file`, `roles` (`:206-222`) — and an
  unknown pointer such as `agent.publications` is simply never read. Confirmed. No throw, no
  warn, no fallback path; it is invisible to them until they choose to read it.
- The `roles` half needs correcting. `agent.roles` does **not** only drive `roleFor(path)`
  (`:310-313`): it also feeds `agentRoles()` (`:306`) and `survivesUpdate()` (`:322-327`).
  Neither is an existence check, so the conclusion holds — but for a reason the claim as
  written did not give. Two facts make the addition inert rather than merely un-checked: none
  of the three view methods has a **production** consumer today (the only references outside
  `layout.ts` anywhere in their tree are in `layout.test.ts:54-66`), and our new entry declares
  `survives_update: true`, which is exactly what `parseRoles` (`:137`) defaults an absent field
  to — so `survivesUpdate('publications.json')` returns `true` both before and after the
  addition. Had the entry declared `survives_update: false`, it would have changed that answer
  even though nothing consumes it yet. Additive is a property of the value here, not only of
  the key.

**16h. `DEFAULT_EXCLUDES` ≡ `layout.json` `cloud_import_excludes`** — equal, same set, same
order, **41 entries**, with `publications.json` at index 11, between `Makefile` and `.claude/`.
`**/publications.json` is absent everywhere: the ledger is root-anchored as a plain string, per
D11's amendment constraint 3 and 15d's warning that an object entry in that list is dropped
silently by `asStringArray`. `DEFAULT_SECRET_FILE_RULES` ≡ `layout.json` `secret_files.rules`:
unchanged, still equal. **This figure supersedes the 40 recorded in 9f and in Bucket 10's
"What held up" list; both now carry a marked correction pointing here.** Re-derive it at the
point of use rather than quoting it forward — see the standing checklist.

**16i. Contradictions found by this run and recorded, not fixed. Each is owned by a later
phase and none was touched here.**

1. **SUPERSEDED — true as of this bucket, false now (see §0 Bucket 19a, and Bucket 22g for
   the re-derivation). The key has been removed from our template and `cmd_new` now RAISES if
   it or `cloud` reappears there; the DESKTOP's bundled copy still carries it, which is what
   keeps the D5 re-bundle ask alive. Kept as written because the pre-fix divergence is the
   evidence trail for that ask — do not read the sentence below as current.**
   **`templates/agent/cinna-agent.json:25` still ships `"publications": []`** — landed Phase 6
   work that R4 contradicts. **Mitigated but unresolved.** Because `cmd_new` now routes
   through `write_manifest`, a *scaffolded* folder never carries the key: verified, the key is
   absent from the scaffolded manifest and no stray `publications.json` is written (nothing is
   absorbed from an empty list, so `ledger_changed` stays false). Only the template **file**
   carries it — and that file is exactly what
   `test_template_manifest_matches_the_shipped_schema` reads. No test status changes:
   `additionalProperties: true` means `jsonschema` does not reject the extra key, and the new
   manifest-side finding is a warning. But **the shipped template is now factually
   inconsistent with the shipped schema**, and both ship inside the contract tarball. Phase
   6/10 to settle — note it interacts with 14g/15g, which already require that test to
   validate a *scaffolded* manifest rather than the raw template.
2. **The desktop's validator becomes wrong at `validator.ts:512-540`** — it validates
   `manifest.publications`, a key that no longer exists on the manifest. Verified against
   their tree: their manifest type carries `publications?: AgentPublication[]`, and
   `manifestIo.test.ts:48,62` and `validator.test.ts:239-243` both pin the manifest shape
   (the latter asserting `manifest.publications.required`). This is a **required desktop-side
   change** — it has been added to that list without an ordinal, and it has its own answer-back
   section above, because the message is narrower than "your validator is wrong": the checks
   themselves are right and keep their severity, only the file they read moves.
   **Citation correction, since it was cited to this run as `manifestIo.ts`:** `manifestIo.ts`
   contains no `publications` reference at all. The type lives in
   `src/shared/kit/manifest.ts:122` (`publications?`), with `AgentPublication` at `:77`; only
   the *test* citations `manifestIo.test.ts:48,62` land in that module's test file.
   **And the read sites are wider than the validator**, which the answer-back must say or they
   will fix one and ship the other: `scannerService.ts:420` reads
   `manifest.publications` into the scanned agent, `src/shared/localAgents.ts:249` types it,
   and `ReadOnlyCards.tsx:233-246` renders it. All of those move to the ledger file with the
   data.
3. **`docs/local_agent_kit/CHANGELOG.md` is still entirely pre-1.0.0, and this is BLOCKING for
   Phase 10.** It still says "Unreleased — manifest `schema_version` 1" and "`kit.py` refuses a
   manifest whose `schema_version` is higher than the one it understands" — which Phase 7 made
   false by deleting `SUPPORTED_SCHEMA_VERSION` (verified: `grep -c SUPPORTED_SCHEMA_VERSION
   docs/local_agent_kit/tools/kit.py` is 0). It carries no Compatibility table, though
   `schema/cinna-agent.schema.json:10`'s identity `$comment` says the rule is encoded in three
   places and "if you change one, change all three".

   Pre-existing, and Phase 10's to fix — but it is filed as **blocking** rather than as a note,
   for a reason that is about the artefact and not about tidiness: `CHANGELOG.md` is a
   **contract tarball member** (`local_agent_kit_service.py:96`, `CONTRACT_MEMBERS` at `:110`),
   so the archive we hand another team currently ships a false statement about the tool inside
   it. That is worse than a stale internal document by exactly the margin separating a wrong
   premise you inherit from one you write down and send. The missing Compatibility table is the
   same defect one turn further: the schema's `$comment` naming three places to keep in step
   **ships in the same tarball** as a changelog that has none of them — an internal
   contradiction visible to a reader who opens two files, and the kind that makes a reader
   distrust the whole tree rather than the one file. **If Phase 10 is compressed or split for
   any reason, this item comes out and runs standalone; it is not trimmed with the rest.**

### Bucket 17 — R4 review findings

**Status: VERIFIED (R4 review pass; every finding reproduced by execution).** Appended
after Bucket 16; Buckets 1–16 untouched. A fix round was in flight while this was written,
so everything below is recorded as a **finding with its reproduction**, never as "fixed" —
a later bucket records what the fix round did. Line citations were re-derived against the
working tree at the moment of writing (`kit.py` md5 `e5fba2ffe86938bdaa056eec5ec5662c`) and
will drift as that round lands; the function names are the stable half.

**HIGH 1 — the two hosts now scaffold different bytes, on the one file whose byte shape is
contractual.**

> **SUPERSEDED on OUR side — true as of this bucket, false now.** Our template no longer
> carries `publications` (nor `cloud`), and `cmd_new` raises if either reappears in the kit's
> own template: see §0 Bucket 19a for the fix and Bucket 22g for the re-derivation. **The
> desktop's bundled copy still carries it**, so the finding's consequence is live and the D5
> re-bundle remains the whole remedy. Kept unedited below because the pre-fix state is the
> evidence trail; the `:25` citation no longer resolves.

`templates/agent/cinna-agent.json:25` still carries `"publications": []`.
Ours strips it: `cmd_new` routes through `write_manifest` (`kit.py:1638`, its sole
production caller), which removes the key on every write, so our scaffolded manifest's keys
end `… handovers, features` — verified by scaffolding one and reading the key list back.
The desktop never calls anything equivalent. Its `buildManifest`
(`src/main/services/localAgents/scaffoldService.ts:151-180`) **parses the template document
and sets seven fields on it**, explicitly "so unknown template keys and key order survive
exactly as `kit.py new` leaves them" (`:148-150`), and `copyTree` skips the manifest so it
is written only through that path (`:134`). Their bundled template ends with
`"publications": []` too, so their scaffold's keys end `… features, publications`.

**Different key sets AND different key order**, on the file Phase 6 step 5 declares
byte-contractual. Three consequences, and the third is the one to lead with: a
desktop-scaffolded folder trips our new manifest-still-carries-`publications` warning
**immediately, on a folder created seconds earlier by the other host implementing the same
contract**; the byte-identity claim Phase 6 rests on is false the moment both scaffolders
run; and the contract tarball ships a **template that contradicts its own schema**, since
`publications` was moved out of `cinna-agent.schema.json` by R4. Bucket 16i.1 recorded the
template as "mitigated but unresolved" on the strength of our scaffold never carrying the
key — correct as far as it went, and this is the half that mitigation does not reach,
because the mitigation lives in *our* writer and the template is what the *other* host
consumes.

**Citation correction, recorded rather than silently applied:** this finding was handed to
the review citing `src/shared/kit/manifest.ts:128-136` as the desktop's substitution
mechanism. That range is real and is `MANIFEST_TOKENS`, but it has **no production
consumer** — `grep -rn MANIFEST_TOKENS src/` returns only its own declaration and the type
alias derived from it. The desktop deliberately does **not** token-substitute the manifest
as text (`scaffoldService.ts:15-18`: "a description containing a quote would produce invalid
JSON"). The mechanism is parse-and-set, and `scaffoldService.ts` is the citation that
supports the claim. The claim survives; the evidence for it moved.

**HIGH 2 — `write_manifest` destroys data it cannot place, and its own docstring promises it
will not.** `absorb` (`kit.py:476-489`) has three early returns — entry not a dict,
`platform_url` not a non-empty string, `platform_url` already in the ledger — and returns
`None` in every case, so the caller cannot tell "absorbed" from "dropped". The two removals
above it (`del document["publications"]` at `:503`, `del document["cloud"]` at `:509`) are
guarded on **type** and never on **outcome**: once the value is the right type it is deleted
whether or not anything received it. Reproduced by execution:

| input manifest | result |
|---|---|
| `cloud: {"agent_id": "REAL-AGENT-ID-42", "imported_at": …}`, no `platform_url` | key deleted, **no ledger written** — the `agent_id` is gone |
| `cloud: {"platform_url": None, "agent_id": None, "imported_at": None}` | key deleted, no ledger written |
| `publications: [{"agent_id": "g1"}]` (no `platform_url`) | key deleted, no ledger written |
| `publications: ["junk"]` (entry not an object) | key deleted, entry vanishes silently |
| `cloud: "junk"` (not a dict) | **left in place** — the type guard works |
| `publications: "foo"` (not a list) | **left in place** — the type guard works |

**It contradicts the function's own docstring** (`kit.py:449-453`), which states that a value
this build cannot read "is LEFT IN PLACE rather than dropped" because "discarding data a host
cannot interpret is unrecoverable, while leaving a deprecated key is visible". That promise
holds for the two rows at the bottom of the table and is false for the four above them. The
combination is worth naming on its own: **a promise in prose beside an unfulfilled guard in
code.** A reader auditing this function by its docstring passes it.

It is also **asymmetric against `read_publications`**, which refuses on exactly this
malformed shape when it appears in the ledger file — an entry that is not an object returns
the `None` tristate and `write_manifest` raises rather than write anything (`kit.py:400-401`,
`:456-467`). So the identical bad data is treated as unrecoverable in one file and as
disposable in the other, inside the same function call.

**Record the PROGRESSION, not the instance — this is the third, and the sequence is the
finding.** Do not file it as "another fail-safe bug".

- Bucket 10 HIGH 1: the fail-safe was applied at **clause** level and never lifted to
  **rule-shape** level.
- This one: applied to values it cannot **type**, and never lifted to values it cannot
  **place** — type level versus **outcome** level.

**The same author-level blind spot each time: the guard is written where the check is, not
where the consequence is.** That is a different statement from the guard-asymmetry heuristic
already in this document, and it is the more useful one, because guard asymmetry is only
visible when the two statements sit side by side — here the guard and the consequence are
in different functions, one of them a nested closure, and nothing about the code's
appearance flags it.

**HIGH 3 — the migration can write a file its own validator rejects, and its own schema
forbids.** A `cloud` block carrying a `platform_url` but a null or absent `agent_id`
absorbs **verbatim**, producing a ledger entry `{"platform_url": "…", "agent_id": null}`.
That violates `publications.schema.json` (`required: ["platform_url", "agent_id"]`, both
typed `string` with `minLength: 1`) and fires `_validate_publications`' error
``publications.json: `publications[0].agent_id` is required.`` — reproduced by running the
validator over the ledger the writer had just produced. Because it is an **error**, it
blocks `export` without `--force`, and `validate` then blames the file the tool itself
wrote. The asymmetry inside `absorb` is the cause: it guards `platform_url` and not
`agent_id`, though schema, `_validate_publications` and the desktop's `checkPublications`
all treat the two as a required pair.

**Correction to the finding as it was handed to the review, and the correction matters
because the two halves land on different findings.** It was recorded as the *all-`None`*
`cloud` shape absorbing verbatim. It does not: with `platform_url` `None`, `absorb`'s second
early return fires and the block is **deleted with no ledger written** — that is HIGH 2's
row, not this one. Both halves of the original claim are true of something; they are true of
different things:

- The **all-`None` shape is the common-in-the-wild one**, and the justification given for it
  checks out: `HEAD`'s template ships exactly `{"platform_url": null, "agent_id": null,
  "imported_at": null}`, and `HEAD`'s `cmd_export` wrote that same literal into **every**
  exported copy (`kit.py:1389` at `HEAD`). Its failure mode is HIGH 2's silent deletion.
- The **partially-populated shape produces the schema violation**, and nothing in either
  host is currently known to write it — so it is a real defect reachable by hand-editing or
  by a future publish path, not a common one.

Recorded this way rather than merged, because "the common shape writes an invalid file" and
"the common shape silently loses data" are different asks of whoever fixes them.

**MEDIUM 4 — constraint (d)'s forbidden branch, and a defect mirroring the one we are asking
the desktop to fix.** `cloud_import_excludes()` (`kit.py:554-566`) falls back to the
hard-coded `DEFAULT_EXCLUDES` whenever `layout.json` is missing, unparseable, or filters to
empty — silently, with no warning and no signal in the output. `cmd_export` then prints a
confident `content_hash` and exits 0. Reproduced: a kit copy whose `layout.json` was replaced
with `{not json`, exported with `--hash`, printed
`sha256:c6561a93…` and exited 0, byte-identical to the run against the intact contract and
with nothing on stdout or stderr distinguishing the two.

15i's constraint (d) is explicit that this is the one forbidden direction: **for anything
affecting a hash, unevaluable ⇒ refuse to emit `content_hash` at all — never emit one
computed a different way.** The function's docstring defends itself — "that fallback is the
same list, so the fallback path cannot change which files travel or what they hash to" — and
the defence is **Bucket 6b's shape**: a sound argument about the wrong artefact. It is a
claim about *this build's* `DEFAULT_EXCLUDES` matching *this build's* `layout.json`; the file
being read is the one in the folder in front of the user, which a `refresh`, a partial
download, a hand-edit or a future contract can make differ. That the two happen to agree
today is why the reproduction prints an identical hash — which is the failure, not the
mitigation.

**Adjacent, and it deserves its own sentence.** The same function's
`[p for p in patterns if isinstance(p, str) and p]` is the **identical silent-shortening
filter** we are asking the desktop to fix — their `asStringArray` (`layout.ts:228-229`),
which the `desktop_owned` answer-back section and 15d both name — applied to the one list
that gates secrets. **We do not get to send that paragraph while shipping the same defect
ourselves.** Fixing ours first is what makes the ask credible rather than hypocritical, and
that is a reason to fix it over and above the defect: the sequencing is load-bearing and
should not be read as incidental.

**MEDIUM 5 — the tristate's "cannot parse" state is unreachable through the encoding door.**
`read_json` goes through `read_text` (`kit.py:353-355`), which is
`read_text(encoding="utf-8", errors="replace")`, so undecodable bytes never raise and
`read_publications`' `OSError`/`JSONDecodeError` handlers never see them. Reproduced: a
ledger holding a latin-1 `https://café.example` was read as
`https://caf�.example` — the tristate returned entries, not `None` — and the next
`write_manifest` **wrote it back mangled** as UTF-8 `\xef\xbf\xbd`. Because `platform_url` is
the dedupe key, a subsequent migration of that same instance no longer matches and appends a
**duplicate entry** for it; verified, the ledger ended with three entries for two instances.
So the failure is not merely lossy display: it silently converts the ledger's primary key and
then double-counts the instance.

**MEDIUM/LOW 6 — guard asymmetry in `_validate_manifest_ledger_keys`
(`kit.py:1104-1142`), and both branches emit a confidently wrong statement.** The `cloud`
branch type-checks (`cloud is not None and not isinstance(cloud, dict)` → error); the sibling
`publications` branch does not — it is `if "publications" not in manifest: return` followed by
an unconditional `report.warn`. Reproduced:

- `{"publications": "foo"}` → the warning *"It moves across on the next write."* It does
  not: `write_manifest` migrates only `isinstance(stale, list)`, so a string is left in place
  forever and the message is false on every subsequent run.
- `{"cloud": None}` → the info *"It still reads, and moves into publications.json on the next
  write."* It does not: migration requires `isinstance(cloud, dict)`.

Both statements are wrong in the same direction — they promise a move that will never happen
— which makes them worse than silence: a user who reads either one and re-runs `write_manifest`
sees the same finding again with no explanation. This is the same guard-asymmetry shape as
Bucket 16d.2, in a different function, and the fail-safe direction the two removals in
`write_manifest` implement is exactly what these two messages fail to describe.

**LOW 8** — stale comment at `kit.py:2717-2719`. It still says the digest comes from the
source "never from the destination, which this command then edits (requirements regenerated,
publication links cleared)". Export no longer clears publication links — 16a deleted that
rewrite, and the comment forty lines below it (`:2771-2782`) now says so at length. Two
comments in one function disagreeing about whether it writes the manifest.

**LOW 9** — no contract-data pointer to `schema/publications.schema.json`. `kit.json`
declares `"manifest_schema": "schema/cinna-agent.schema.json"` and nothing for the ledger.
The file **does** ship in the contract tarball, because the backend selector is prefix-based
(`CONTRACT_MEMBER_PREFIXES = ("schema/", "templates/")`,
`local_agent_kit_service.py`), so this is a discoverability gap and not a packaging one: a
consumer reading the contract *as data* cannot find the ledger's schema, and only a consumer
listing the tarball can. Note `layout.json`'s `agent.roles` entry for `publications.json`
does name the path in prose inside its `description`, which is not a machine-readable
pointer.

**LOW 10** — `publications.schema.json` declares `"required": ["publications"]` while
`_validate_publications` reports a missing key as a **warning**, arguing in the code that "an
object with no `publications` key is a file that says nothing, not a file that says something
wrong". Left deliberately: the divergence is in the **softer** direction, which by the D13
ranking degrades noisily and recoverably rather than silently. Recorded so it is not
discovered later and read as an oversight.

**LOW 11** — `agent.publications` is decorative on both hosts, exactly as `agent.manifest`
already is. `_layout_agent_path` (`kit.py:589-596`) is called only for `command_catalog` and
`status_file`; `MANIFEST_NAME` and `PUBLICATIONS_NAME` are module constants
(`kit.py:64`, `:75`) that no code path resolves through the contract. So the pointer 16g added
buys the D15/D6 principle in *declaration* and not yet in *behaviour*, on our side as much as
theirs. Same status as `agent.manifest`, which has been decorative since it was written — this
is a note, not a regression.

**LOW 12** — a corrupt ledger shows `CLOUD: yes` in `cmd_list` with no signal.
`is_published` (`kit.py:406-422`) returns `True` when `read_publications` returns the `None`
tristate, and argues the choice in its own docstring ("the file only exists because something
published; reporting 'no' would be a confident wrong answer where 'yes' is merely
imprecise"). The reasoning holds; the gap is that `cmd_list` (`:2208`) collapses it to a bare
`yes` in the `CLOUD` column with nothing marking the imprecision, so the one surface a user
looks at cannot distinguish "published" from "published, and the record of it is unreadable".

**What this review confirmed SOUND — recorded so no later reader re-verifies it.**

- **Export does not rewrite the manifest.** Measured, not read: on a folder carrying a
  populated root `publications.json`, source and destination `cinna-agent.json` md5 are
  identical (`82e4bd0d…`). 16a's byte-parity claim holds at this commit.
- **Migration moves the hash exactly once and then never again**, verified across two
  publishes — 16c's `h0 ≠ h1`, `h1 == h2` reproduces.
- **Copy set == hashed set**, and `ALWAYS_EXCLUDE` remains fully redundant against the
  shipped list. 7f and 9f still hold at 41 entries.
- **Root-anchored exclusion works and does not over-reach.** The root `publications.json` was
  withheld while a nested `files/publications.json` still travelled, in the same export run.
  This is the property that makes the plain-string form correct and `**/publications.json`
  wrong, and it is now measured rather than argued.
- **The kept existence assertion still fires** (`kit.py:2767-2770`), proven by 16a's data-only
  mutation — a `layout.json` that swallows the manifest, with `kit.py` byte-identical.
- **The tristate is complete on every door except MEDIUM 5's.** Missing file, `OSError`,
  `JSONDecodeError`, non-dict document, non-list `publications`, non-dict entry all land
  correctly; only the encoding door is unreachable, and only because `read_text` replaces
  rather than raises.
- **The moved schema block is verbatim, key order included.** Re-derived against the
  desktop's authored file rather than against `HEAD`, because `HEAD` has no comparison base —
  the block is itself uncommitted Phase 2 work, exactly as 16e's caveat says.
- **`parseLayout` tolerates both `agent` additions**, `publications` and the `roles` entry, for
  the reasons 16g gives — cross-referenced, not re-derived here.

**The D11 `write_manifest`-has-no-caller question: RESOLVED by the team lead, recorded as a
resolution and not as an open item.**

The structural fact first, because the resolution rests on it: `write_manifest` has exactly
one caller, `cmd_new` (`kit.py:1638`). No `kit.py` command rewrites an existing manifest.
16b framed the single-writer property as what makes the migration structural — "a write path
added later cannot forget the migration" — and that is true, but it has a second consequence
16b did not draw: **the migration path is dead today.** Nothing a user can run re-stamps an
existing folder, so the D11 amendment's "the remedy is the re-stamp already being asked for"
(echoed verbatim in `write_manifest`'s own docstring, `kit.py:433`) names an action a user
cannot currently perform.

**Ruling: soften the D11 amendment's wording; do NOT add a `migrate` verb.** The kit is
unreleased, neither host has a publish path, and nothing anywhere writes `publications[]` — so
the population of folders needing a re-stamp today is approximately our own dev tree.
Inventing user-facing surface for a user who does not exist is the tail wagging the dog. The
amendment's wording will change to say plainly that **there is no re-stamp a user can perform
today**; an escape hatch naming a fictional action is worse than one that admits the gap,
because the first stops anyone looking for the gap. The requirements document is being edited
separately to carry that wording — reference the resolution from here, do not restate the new
text as though it were authored in this bucket, and do not edit that document from this plan.

**The migration code stays, and stays correct — record the reasoning, because the obvious
objection is "then why keep it at all".** Precisely *because* the path is dead: when a publish
path arrives and becomes its first real caller, nobody will remember this function was never
exercised, and both HIGH 2 and HIGH 3 are the silent-data-loss kind that surface once, in a
user's folder, irreversibly. **Dead code that will certainly be woken is worse than live code,
because it carries the appearance of having been used.** That sentence is the transferable
part, and it is the reason HIGH 2 and HIGH 3 are filed at HIGH severity against a function no
user reaches today.

**Citation corrections from this pass, listed rather than silently applied**, per the standing
checklist. The finding each supports survives in every case; only the evidence moved.

- `scannerService.ts:419` → **`:420`**. `:419` is the bare `manifest,` property; the
  publications read is the line after. Bucket 16i.2 and the required-changes list already
  carry `:420` and are right.
- `checkPublications` `validator.ts:504-545` → **`:504-542`**; the early return that produces
  the silent-misreport is `:513`.
- `absorb` `kit.py:465-484` → **`:476-489`**, and it has **three** early returns, not four.
- `cloud_import_excludes()` `kit.py:553-566` → **`:554-566`**.
- `_validate_manifest_ledger_keys` `kit.py:1111-1144` → **`:1104-1142`**.
- `manifest.ts:128-136` cited as the desktop's manifest substitution mechanism — the range is
  correct for `MANIFEST_TOKENS`, but that constant has no production consumer and the desktop
  does not substitute the manifest as text; see HIGH 1.
- `templates/agent/cinna-agent.json:25`, `kit.py:1638`, `kit.py:2717-2719`,
  `manifest.ts:77`, `manifest.ts:122`, `layout.ts:228-229` and `kit.py:1145-1194` all check
  out exactly as cited.

**A further contradiction found and NOT fixed, reported here per the review's terms.** The
"34 exclude patterns" figure is wrong in **both** places it appears, not merely stale.
Re-derived by loading both files: theirs is **32** and ours is **41**. The D5 re-bundle
bullet in the required-changes list has been corrected in place with the strike-through the
standing checklist requires; **Bucket 15e:2 still says 34 and was deliberately left
untouched**, because Buckets 1–16 are closed to this pass. Whoever writes the answer-back
must take the corrected figure from the D5 bullet, not from 15e. Two riders on the same
sentence, also left standing in 15e: its "both forms of the last three" gloss is wrong for
`__pycache__` — they carry `**/__pycache__/` and lack only the root form, which is precisely
the unpaired-entry hole the `**/`-prefixed-directory-pattern section describes — and the list
of what theirs lacks now needs `publications.json`, a gap R4 opened after 15e was written.

**Re-derivation of the 32/41 correction, and the reason it is filed as a DIFFERENT failure
from a stale figure.** Both numbers re-derived at this point of use by loading the two
`layout.json` files (`docs/local_agent_kit/layout.json` and
`/Users/evgenyl/dev/ml-llm/cinna-desktop/resources/cinna-kit-contract/layout.json`) and
counting `cloud_import_excludes`: **ours 41, theirs 32**, both files declaring
`"contract_version": "1.0.0"`, theirs a **strict subset** of ours (nothing in theirs is
absent from ours). The nine ours has and theirs lacks: `publications.json`, `temp/`,
`credentials/`, `**/*.env`, `__pycache__/`, `.mypy_cache/`, `**/.mypy_cache/`,
`.ruff_cache/`, `**/.ruff_cache/`. Theirs also has no `secret_files` block and types
`desktop_owned` as strings where ours is objects — re-derived in the same load.

**"34" was WRONG WHEN IT WAS FIRST WRITTEN. It never described any state of either file.**
Say so wherever the correction is recorded, because the conclusion a reader otherwise draws
is the damaging one: **the standing checklist's stale-figure rule would not have caught
this.** That rule protects against a figure that was true in its own bucket and became false
in transit — it says re-derive at the point of reuse, and a faithful re-derivation of a
number that was never true simply produces the same wrong number if the deriver copies the
method instead of the measurement. What caught this was loading the files. A rule credited
with catching things it cannot catch is worse than no rule, because the credit is spent
before the gap is noticed: readers stop looking for the class of error the rule does not
cover. The two lessons are therefore distinct and must not be merged — **stale** is a
transport failure, **wrong-when-written** is a measurement failure, and only the second is
addressed by re-deriving from the artefact rather than from a prior statement about it.

**Both riders belong wherever the corrected number goes, and the D5 re-bundle bullet — the
sanctioned point of reuse — carries them; verified, not assumed.** Checked at this pass: the
D5 bullet states the `__pycache__` rider correctly (they carry `**/__pycache__/` and lack
only the root form) and lists `publications.json` first among the nine, noting it as a gap R4
opened rather than one of the D14/D15 additions. A corrected number sitting beside a wrong
gloss is barely an improvement, so the pairing is the requirement, not the number alone.


### Bucket 18 — the coordination layer as instrument: a passing reproduction of the wrong defect

**Status: VERIFIED as a near-miss (no wrong fix shipped). Appended after Bucket 17; Buckets
1–17 are not rewritten by this pass except at Bucket 17's own corrections area, which the
ruling opened.** Do not assert an ordinal for this in the instrument-failure family — see
the standing checklist's rule on ordinals.

**The instrument-failure record so far concerns AGENTS MEASURING THE WRONG ARTEFACT. This
one has a new host: a BRIEF pointed an implementer at the wrong defect.** That difference is
the finding, not a detail of it.

**What happened.** The coordinator's fix brief restated Bucket 17 HIGH 3's trigger as an
all-`None` `cloud` value — `{"platform_url": None, "agent_id": None, "imported_at": None}`.
That value is **HIGH 2's** trigger, not HIGH 3's: with `platform_url` null it hits an early
return in the absorb step and is never absorbed, so the manifest key is deleted with no
ledger entry written — the silent-data-loss row. HIGH 3 needs the *partially populated*
shape: a real `platform_url` string with `agent_id` null or absent, which absorbs verbatim
and produces a ledger entry the schema and both validators reject.

**Why it is a near-miss and not a typo.** A fix agent following that brief would have built a
HIGH 3 reproduction from the all-`None` shape, **watched it reproduce, and concluded HIGH 3
was covered while having reproduced HIGH 2.** The reproduction passes. The reproduction is
real. It is a reproduction of a different defect. Record the phrase, because it is the
compact form of the whole finding:

> **A passing reproduction of the wrong defect.**

**Same disease, new host.** Every earlier instance in this document is an agent measuring the
wrong artefact — a sound instrument pointed at the wrong input (15f), a vacuous green, a
broken tool. This one is the **coordination layer**, not a test harness: the artefact
substitution happens in the sentence handing the work over, before any instrument is built.
The mitigation must therefore cover **briefs**, not only test runs — which is exactly what
the new standing-checklist rule on implementation briefs does. That rule and this finding are
one item; neither is complete without the other.

**Evidence, and its honest limits.** Bucket 17's own HIGH 3 correction paragraph records the
same split ("with `platform_url` `None`, `absorb`'s second early return fires and the block
is **deleted with no ledger written** — that is HIGH 2's row, not this one"), and the split is
independently corroborated in the working tree by the fix round's own prose:
`ledger_entry_is_placeable`'s docstring (`docs/local_agent_kit/tools/kit.py`) states that "a
`cloud` block whose `platform_url` is ALSO null never reached the ledger at all, so it was
the data-loss defect rather than this one". **The pre-fix mechanism itself can no longer be
re-executed from this tree** — the fix round has landed, `absorb` no longer exists under that
name (the migration is now the `migrate` closure inside `write_manifest`, guarded by
`ledger_entry_is_placeable`), and `HEAD` predates R4 entirely, so it has no `absorb` either.
Recorded as corroborated-by-two-independent-prose-sources rather than as re-executed, per the
standing checklist's preference for "I could not prove this" over an unproven green.

### Bucket 19 — R4 review-fix round

**Status: VERIFIED (R4 review-fix run).** Appended after the coordination-layer bucket;
Buckets 1–17 are not rewritten by this pass except Bucket 11's status line, which the D15
finding below required and which Task 2 opened. That status edit changes the marker only —
Bucket 11's original text is left standing.

**Numbering, reported rather than silently corrected.** This bucket was commissioned as
"Bucket 18". That number was already taken, by the coordination-layer bucket appended
between the commissioning and this run. Renumbering that one would break the references to
it in the standing checklist and in its own cross-links, and Buckets 1–17 are closed to this
pass, so this bucket takes the next free number instead. Both buckets record the same fix
round from different angles: that one records the brief that pointed at the wrong defect,
this one records what the round actually landed.

**Verification basis for everything below.** Claims were checked against
`docs/local_agent_kit/tools/kit.py`, `docs/local_agent_kit/kit.json`,
`docs/local_agent_kit/layout.json`, `docs/local_agent_kit/templates/agent/cinna-agent.json`
and the desktop's `scaffoldService.ts`, plus a behavioural run of `write_manifest` over
legacy shapes in a scratch tree. Three claims in the round's own report did **not** hold as
stated and are recorded as such rather than repaired — see **19j**.

**19a — HIGH 1 (the template's `publications` key).** The key is removed from
`templates/agent/cinna-agent.json`. It was the **last** key in the document, so no other
key's order moved — the template now ends `… handovers, features`, which is exactly where
our own scaffolded manifest already ended, because `write_manifest` was stripping the key on
every write regardless. **So our scaffold output is unchanged by this fix. Only the
desktop's output changes, which was the point:** the divergence lived in the artefact we
hand them, not in our writer.

The corrected mechanism belongs here rather than only in Bucket 17's citation-correction
list, because it is what makes the ask sayable. The desktop does **not** token-substitute
the manifest as text. `buildManifest` in
`/Users/evgenyl/dev/ml-llm/cinna-desktop/src/main/services/localAgents/scaffoldService.ts`
is parse-and-set, and its doc comment states the intent in its own words. Verified verbatim
against their source at **`scaffoldService.ts:148-149`** (the two comment lines; the
`function buildManifest(` signature is at `:151`, and the call site at `:253`):

> Build the manifest from the template document, so unknown template keys and
> key order survive exactly as `kit.py new` leaves them.

Bucket 17 cited that comment as `:148-150` and the answer-back section as `:148-149`; the
latter is exact. Preserving the template faithfully is precisely what made their scaffold
diverge from ours while the template still carried the key — which is why the fault is ours
and the remedy is the D5 re-bundle already on the required-changes list, not a new ask.

**The secondary fix, and it is the more durable half.** `cmd_new` now **raises** if the
kit's own template carries `publications` or `cloud`
(`ledger_keys = [key for key in ("publications", "cloud") if key in manifest]` → `KitError`
naming `TEMPLATE_DIR` and telling the user it is a defect in the kit, not in anything they
did). The migration therefore no longer doubles as the only thing hiding a broken kit: with
the repair alone, a stale template would have been silently stripped on every `new`, for as
long as it stayed stale, with nothing ever saying so — and stripped only for the host that
routes through `write_manifest`, which is not the desktop. **An assertion replacing a silent
repair** — the same shape the "a filter used where an assertion belongs" section records
three earlier instances of, and `cmd_export`'s manifest-existence assertion says so in its
own comment.

**19b — HIGH 2 (data destroyed before it landed).** `absorb` is gone, replaced by a per-key
all-or-nothing `migrate` closure inside `write_manifest`. The manifest key is deleted only
after **every** entry it holds is placeable, and nothing is written to disk before both keys
have been decided, so a declined migration leaves no half-moved state. Verified by
execution over a scratch tree: a `cloud` block with an `agent_id` and no `platform_url`, an
all-`None` `cloud`, a `cloud` carrying `platform_url` but a null `agent_id`, and a `cloud`
whose optional `imported_at` is `123` all leave the key **in the manifest** with no ledger
written; a fully populated `cloud` migrates and the key goes; a `publications` array holding
one unplaceable entry moves nothing and stays whole.

**Record the departure from the coordinator's instruction, and that it was endorsed.** The
coordinator specified raising `KitError` on an unplaceable value. The implementer chose
**leave-in-place** instead, and the reasoning is worth keeping rather than the verdict alone:
raising would make `write_manifest` — and therefore `validate --fix`, and therefore `new` —
hard-fail on any legacy folder, an unacceptable blast radius for a condition that destroys
nothing. Leave-in-place is also what the function's own docstring already committed to, and
the original finding was precisely that the fail-safe had been applied to values the build
cannot *type* and never lifted to values it cannot *place*. Raising would have been a third
policy in the same function. **Loudness is preserved at `validate`**, which is the only path
with a reporting channel; the writer stays silent because a writer has nowhere to say it.

**19c — HIGH 3 (a ledger failing its own schema), and the asymmetry was wider than
reported.** The trigger is a **partially populated** `cloud`: a string `platform_url` with
`agent_id` null or absent. It is **latent, not common — nothing writes that shape today**,
so it is reachable by hand-editing or by a future publish path. A `cloud` whose
`platform_url` is *also* null never reaches the ledger at all; that is HIGH 2's row, not
this one, and the two are recorded separately because they are different asks.

**A third trigger the review did not name.** `imported_at` as a non-string — `123`, say —
also produced a ledger entry `_validate_publications` rejects, by its
`must be a string or null` rule. So the old guard covered **one field of seven**: two
required (`platform_url`, `agent_id`) and five optional strings (`workspace`, `imported_at`,
`updated_at`, `contract_version`, `content_hash`). "It guarded `platform_url` but not
`agent_id`" understates it. (This third trigger is argued from `_validate_publications`'
current rules and from the fixed predicate's own docstring, which records it; the pre-fix
`absorb` no longer exists in the tree and `HEAD` predates R4, so it could not be
re-executed — the same evidentiary limit the coordination-layer bucket records.)

**Approach: refuse, not repair.** `agent_id` cannot be invented; dropping the entry to make
the file valid would discard the `platform_url` record, which is the unrecoverable
direction; and writing `null` is the defect itself. So the entry stays in the manifest,
where the data is intact and `validate` names it.

`ledger_entry_is_placeable()` mirrors the complete per-entry rule, and `_validate_publications`
now reads its key lists from the same two module constants (`LEDGER_REQUIRED_KEYS`,
`LEDGER_OPTIONAL_STRING_KEYS`). **So "what we will write" and "what we accept" cannot
drift** — a structural fix rather than a remembered rule, and the same move as making
`write_manifest` the sole writer.

**19d — MEDIUM 4 (constraint (d) and the silent filter).** `contract_exclude_patterns()`
returns the contract's list or **`None`** when this build cannot evaluate it. The two
questions that used to share an answer are now split at the call site: `cmd_export` reads
the tristate **once**, sets `hashable` from it, and derives the travel list from the same
read — so the file set that travels and the decision to emit a hash cannot come from
different states of the file.

- **With `--hash`, unevaluable raises before a single file is copied** — the refusal sits
  above `destination.mkdir`, so no destination is created and there is no half-written
  export to clean up.
- **Without `--hash`, the export proceeds on the fallback** and the summary prints
  `content_hash WITHHELD —` **naming the file (`layout.json`) and the reason**, and saying
  the listed files still travelled on the built-in list. Named rather than omitted, because
  a summary that simply stops printing a line the user has seen before is how "no hash"
  becomes "I did not notice".

**The rationale is worth keeping, because the obvious objection is that this is
inconsistent.** Constraint (d) says refuse to emit a *hash*, not refuse to *export*. The
export is still correct and useful; only the number is unanswerable.

**The adjacent silent-shortening filter is gone.** A list holding any non-string or blank
entry now returns `None` whole rather than the usable subset — the same shape we are asking
the desktop to remove from `asStringArray`, on the one list that gates secrets.

**19e — MEDIUM 5, fixed one layer up from where it was found.** `read_text` was left alone;
its callers were checked and every one reads prose that is never written back — a VERSION
file, a `CONTRACT_VERSION`, a gitignore, a `pyproject.toml`, a Makefile, a requirements
file, a command catalog, a status file's frontmatter, a workflow prompt. Its docstring now
says the lossy policy is safe only because of that, and points at `read_json` for anything
this tool writes back.

The fix is at **`read_json`**, which now reads bytes and decodes strictly — because the
manifest makes the same round trip and had the identical defect one file over, so fixing
only the ledger reader would have left the same corruption path open on
`cinna-agent.json`. The failure surfaces as **`JSONDecodeError`, not `UnicodeDecodeError`**,
so every existing handler catches it unchanged and **no call site needed touching**;
verified by execution — a latin-1 `https://café.example` now yields
`JSONDecodeError: the file is not valid UTF-8 (invalid continuation byte at byte 29)` where
it previously parsed with a U+FFFD in the dedupe key. The justification is recorded in the
docstring and is not a disguise: JSON is defined to be UTF-8 (RFC 8259 §8.1), so a non-UTF-8
file is not valid JSON.

**19f — MEDIUM/LOW 6.** Every message in `_validate_manifest_ledger_keys` now **predicts**
using `ledger_entry_is_placeable` — the same predicate the migration runs — so prediction
and behaviour are the same code and cannot say different things. The unplaceable branches
now say the key *stays* and nothing is discarded, instead of promising a move that would
never happen.

`"cloud": null` gets **its own warning** ("the key records nothing and will never migrate;
remove it") rather than an error, and the reason is parity, not leniency: a JSON null for a
cleared optional block is a plausible user act, and a kit-only error over it would breach
§9.2. The non-object `cloud` case stays an error, because there the manifest schema does
have a rule to be in breach of.

**19g — LOW 8, LOW 9, and the `layout.json` note.**

- **LOW 8.** The stale comment is corrected: the digest comes from the source, "never from
  the destination, which this command then edits (workspace_requirements.txt regenerated)"
  — the "publication links cleared" half is gone. The second, **past-tense** mention forty
  lines below was correctly left standing: it says export *used to* clear the links and
  tells the next author not to reintroduce a rewrite, which is a live instruction, not a
  stale claim.
- **LOW 9.** `kit.json` gained `"publications_schema": "schema/publications.schema.json"`,
  beside the existing `manifest_schema` — so a consumer reading the contract *as data* can
  now find the ledger's schema without listing the tarball.
- **The `layout.json` note, and the framing is the fix.** The `publication_ledger` role
  description now states that the file **must survive a kit update** — "it is the user's own
  record of where their agent was published, and a refresh of the kit has no business
  rewriting or removing it" — **written as a requirement about the ledger rather than as a
  note about anyone's parser or refresh implementation.** A workaround described in terms of
  the code that needs it is removed by the next person who concludes that code no longer
  needs it; a requirement about the artefact survives them.

**19h — the invariant, enforced at two points.** **The tool cannot write a
`publications.json` that its own validator rejects.** Both halves are needed and both are
present in `write_manifest`:

- nothing invalid is ever **added** — `migrate` places an entry only if
  `ledger_entry_is_placeable`; and
- a **pre-existing** ledger holding an entry we would not have written **stands the whole
  migration down** (`ledger_writable`) rather than being appended to and rewritten, which
  would make this tool the author of a file its own validator fails.

Verified by execution across four legacy `cloud` shapes (missing `platform_url`; all-`None`;
`platform_url` with null `agent_id`; valid pair with `imported_at: 123`) plus a pre-poisoned
ledger: with a ledger already holding `{"platform_url": …, "agent_id": null}`, a perfectly
good `cloud` block was **not** migrated and the ledger was left byte-untouched. Separately,
a ledger this build cannot read at all still raises rather than being overwritten.

**19i — a new fail-safe pair, and the pair is correct by consequence.**
`contract_exclude_patterns()` and `secret_file_rules()` read two lists out of the same
contract file and now fail in **opposite** directions: the first returns `None` on anything
unreadable (**withhold the number**), the second returns the declared list unfiltered so the
consumer's own fail-safe can fire (**withhold the file**). Both resolve toward the safe
consequence for their own list, which is D15's `match`/`unless` pairing one level up — the
direction is derived from what happens if you are wrong, never copied from a neighbour. A
single policy applied to both would ship a credential or emit an authoritative wrong hash,
depending on which way it was set.

**Recorded as a gap, not as done:** the round's report says both sites "carry a note so
nobody harmonises them". They do not — see 19j.

**19j — claims from the round's own report that did NOT hold, reported and not repaired.**
Per the review's terms; the finding each supports survives in every case.

- **"Both sites carry a note so nobody harmonises them" (19i) — FALSE as of this pass.**
  Each docstring documents its **own** fail direction well, but neither names the other, and
  no comment anywhere in `kit.py` describes the two as a deliberately opposed pair. Grep for
  the cross-reference returns nothing: `contract_exclude_patterns`' docstring cites
  `scaffold_ignore_files()` and the desktop's `asStringArray`; `secret_file_rules`' cites
  `is_secret_filename` and `DEFAULT_SECRET_FILE_RULES`. **The protection the claim describes
  does not exist**, which matters exactly as much as the claim said it would: the two
  functions look inconsistent to a reader who meets them cold, and looking inconsistent is
  what invites a harmonising edit. Left for whoever owns the next pass on that file.
- **"all 12 callers checked" (19e) — the count does not hold.** `read_text` has **ten** call
  sites in `kit.py` today (excluding its own definition and body, and excluding one direct
  `path.read_text(...)` that does not go through the helper). The substantive claim — every
  one of them reads prose that is never written back — checks out on inspection; only the
  figure is wrong. Re-derive it at the point of use, per the standing checklist.
- **"all nine existing handlers" (19e) — the count does not hold.** There are **eight**
  `except` clauses naming `json.JSONDecodeError` in `kit.py`. The substantive claim — that
  surfacing the failure as `JSONDecodeError` means none of them needed touching — checks
  out; only the figure is wrong.

**Two figures re-derived at this point of use rather than quoted forward**, per the standing
checklist: `layout.json`'s `cloud_import_excludes` is **41** entries and
`contract_exclude_patterns()` returns them as a list (not `None`) against the shipped
contract. `ledger_entry_is_placeable` reads **seven** keys.

**19k — the D15 guard asymmetry: the ask we sent before our own code could keep it.** The
full framing, per the team lead's ruling, is in the answer-back section "a filter used where
an assertion belongs" above; this is the evidence trail and the placement decision.

`is_secret_filename` returns `True` immediately for a rule that is not a dict — "a rule this
build cannot even open is a rule it must assume protects something" — and its docstring
promises that a contract declaring rules this build cannot read "withholds everything
instead, which is loud and recoverable". `secret_file_rules()`, one function upstream and the
sole supplier of those rules, filtered non-dict entries out of the declared list before the
consumer ever saw them. **The consumer's fail-safe was unreachable and the docstring's claim
was false.** Fixed by removing the filter: the declared list is now returned as declared,
unreadable entries included. Union-with-defaults was considered and rejected — the consumer
already had the correct documented behaviour, so the fix is to stop suppressing it rather
than to add a second mechanism beside it. Verified: `is_secret_filename(".env", rules=["junk"])`
returns `True`.

**Why this is filed as an honesty item and not as a hardening item.** D15 is on the
required-desktop-changes list, and the guarantee we asked them to adopt is exactly the one
our own implementation could not deliver. The ask still stands and is still right; the record
must carry both.

**Bucket 11's mutation table would have PASSED, and that is the reusable half.** Its ten rows
mutate rule *contents* — `match` renamed, `match` a list, `basename_equals: [123]`, an
unknown key inside `match`, an empty `match`, an absent `match`, `unless` a list, `unless`
with an unknown key — plus one row, "rule a string", which is a shape row. Every contents row
survives an `isinstance(rule, dict)` filter and reaches the consumer, so every one of them
lands correctly whether the filter is there or not. The defect lived in rule **shape**, which
the filter removes. **A correct test of the wrong layer** — the same family as the
coordination-layer bucket's "a passing reproduction of the wrong defect", one level down: not
a wrong reproduction, a right one aimed at a layer that was never in doubt. Bucket 11's
status line is marked accordingly, in place, without rewriting its text.

**How it was found, recorded because the mechanism is the transferable part.** A fix round
re-derived the fail-safe claim from the code instead of trusting Bucket 11's record of it.
Nothing about the two functions' appearance flags the problem — the guard and the thing it
guards are in different functions, and both docstrings read as correct in isolation. Reading
the docstring and believing it is what passes.

**Placement decision for the answer-back, stated so it is not re-litigated by default.**
Considered for a standalone ranked section and folded into the existing "a filter used where
an assertion belongs" section instead, for two reasons: a new top-level section would read as
a **new** desktop-side ask when D15 is already on the required-changes list and none of it
changes; and the finding is another instance of precisely the shape that section exists to
name, so it strengthens the section rather than competing with it. The lead's
do-not-soften ruling is carried into that section verbatim in substance, including the
instruction to test adoption **at the shape layer**.

**Carry-forward: the stale knowledge mirror is the highest-value item in Phase 11, and it is
BLOCKING.** The synced mirror at
`backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/` is
stale for this entire feature. Verified by `diff -rq` against `docs/local_agent_kit/` at this
pass: **`layout.json`, `CONTRACT_VERSION` and `schema/publications.schema.json` are absent
from it entirely**, and a further set of files differ — `kit.json`,
`schema/cinna-agent.schema.json`, `tools/kit.py`, and the agent template's `cinna-agent.json`,
`AGENTS.md`, `Makefile`, `README.md`, `pyproject.toml`, `docs/WORKFLOW_PROMPT.md` and the
`config/`, `credentials/`, `knowledge/`, `scripts/` READMEs. (It also still carries the stale
`tools/__pycache__` Phase 11 item 8 records; not touched here.)

**Record it as a LIVE FALSE-GREEN SOURCE, not merely as a deployment gap — the framing is the
point.** `backend/tests/unit/test_local_kit_tool.py::_find_kit_dir` falls back to this tree,
so a test run that resolves to it exercises a kit without `layout.json`, without
`publications.json`'s schema, and with a pre-R4 `kit.py` — and reports green. That is Bucket
5f's shape (`docs/` unmounted in the backend container is precisely the condition that makes
the fallback fire), except that here the snapshot is stale by a whole feature rather than by a
few files.

**One correction to the carry-forward as it was handed to this run, reported rather than
silently applied.** It was framed as "the fallback tree the test reads when
`LOCAL_AGENT_KIT_DIR` is unset". `_find_kit_dir` has **three** rungs, not two: the env
override, then the repo checkout at `docs/local_agent_kit/`, then the snapshot. With the
override unset **and** `docs/` present, the test reads the working tree and is fine. The
snapshot is reached only when the override is unset **and** `docs/local_agent_kit/kit.json`
is unreachable from the test file's parents — which is exactly the backend container, where
`make test-backend` runs. The corrected form makes the finding *stronger*, not weaker: the
false green is not an odd configuration a developer might hit, it is the default in the one
environment the project's own test command uses.

**Carry-forward: the Phase 10 CHANGELOG item is still marked blocking — confirmed, changed
nothing.** Phase 10 work item 3 still reads "**CHANGELOG.md — BLOCKING, and it does not
travel with the rest of this phase.** If Phase 10 is compressed, split or deferred for any
reason, **this step comes out and runs standalone.**", with the contract-tarball-member
argument and the two false statements intact. Not softened.

### Bucket 20 — Phase 8 findings (`kit.py chat`)

**Status: VERIFIED (Phase 8 implementation run).**

**Verification basis, and it matters for this bucket more than for most.** Everything below
was checked against `docs/local_agent_kit/tools/kit.py` in the working tree, and the two
token-egress differentials were **re-run independently by this recorder** rather than taken
from the implementation report — the standing checklist's rule that a claim describing a
PROTECTION has no second reader is exactly what these are. Method: a scratch harness of two
loopback `http.server` instances, a real `desktop.json` holding a marker token, and a copy
of `kit.py` with one guard removed per run (copies made in a scratch directory; the working
tree was not edited). Three of the round's own claims did not hold exactly as handed over;
they are recorded as such below rather than repaired.

**20a — two token-egress paths found and closed, each proven load-bearing by a reverted-copy
differential.** The highest-value items in this bucket, and the ones that also go to the
desktop as an answer-back section of their own, because the property is a property of HTTP
clients rather than of our code.

- **Redirects.** `urllib`'s default `HTTPRedirectHandler.redirect_request` copies the
  request headers onto the redirected request, dropping only `Content-Length` and
  `Content-Type` — so `Authorization: Bearer <agent_token>` **survives a cross-host 302** —
  and for a POST, 301/302/303 are auto-followed (as a GET). Differential: with
  `_NoChatRedirect()` removed from the opener, the foreign host received exactly one request
  carrying the marker token verbatim in `Authorization`. With the guard in place the foreign
  host received nothing and the verb exited 1 with `HTTP 302 Found`.
  **Sharper than the round reported it, so recorded here rather than as handed over:** the
  reverted run did not merely leak, it **exited 0 and printed the foreign host's answer**.
  The leak's symptom is a successful-looking chat. That is the silent-success shape this
  document keeps naming, in its most expensive instance.
- **Proxies.** A default opener honours `http_proxy` / `https_proxy` / `all_proxy` from the
  environment, and `no_proxy` does not reliably cover loopback. Differential: with
  `ProxyHandler({})` removed, the recording proxy received the request with the marker token
  in `Authorization`; with it in place the proxy received nothing.

The fix is an explicit opener — `urllib.request.build_opener(ProxyHandler({}),
_NoChatRedirect())`, `functools.lru_cache`d — and `_NoChatRedirect.redirect_request` returns
`None`, which makes urllib fall through to `HTTPDefaultErrorHandler` and raise the 3xx as an
`HTTPError` that `cmd_chat` reports as the non-2xx it is.

`_network_reason` exists specifically because `HTTPError` and `URLError` carry `url` /
`filename` attributes of their own, so `str(exc)` on this path can stringify the request URL —
and the request URL is `desktop.json` content. It reads `exc.code` / `exc.reason` /
`exc.strerror` and **never stringifies the exception whole**. `_chat_url` refuses a
`base_url` that is not `http://` or `https://` for the same reason, one layer earlier:
`Request()` raises a `ValueError` that quotes the URL back, and `urllib` will open a `file://`
URL if handed one.

**20b — the token-secrecy assertion covers three things, not one.** The scenarios assert over
**combined stdout+stderr** that the token is absent, that any other `desktop.json` value is
absent, **and that the `api_base_url` itself is absent** — because D3 says a tool never prints
the file's contents and the URL *is* contents. The third one is the non-obvious part and is
the reason it is written down: a reader who implements "never print the token" alone will
print the URL in an error line and think the rule is kept.

**Reported, not repaired: the scenarios themselves are not in the tree.** No committed test
exercises `chat` — `CHAT_TEXT_KEYS`, `cmd_chat` and `_stream_chat_response` have no reader
outside `kit.py`, and tests are Phase 12's. So "every scenario asserts" is a claim about a
harness that no longer exists, and it cannot be checked the way the code can. What this
recorder could check, it did: the design half is visible in the source
(`_read_desktop_connection`'s docstring commits to naming the FILE and at most the KEY, never
a value, a parse position, or the exception raised while reading; nothing in it formats
`data`, the token, or a caught exception — the `json.JSONDecodeError`/`OSError` branch raises
a literal reason with `from None`), and the behavioural half was re-run: in the guard-in-place
redirect run above, neither the marker token nor the `api_base_url`'s host:port appeared
anywhere in combined output. **Phase 12 owes these three assertions a committed home**; until
then the property holds and the evidence for it does not.

**20c — `desktop_state_file()` is a tristate, and the third state is the point.** Block absent
⇒ the built-in default, because a kit predating the block is a kit from before the file could
have moved, and the built-in value is what that contract said. Block present but unreadable ⇒
`None`, and the caller refuses with one line and a `kit.py refresh`. The direction is derived,
not copied: guessing means reading a file the contract no longer names and POSTing a bearer
token to an address found inside it, so loud-and-recoverable wins — and refusing still honours
the rule that matters most for this verb, that it never falls back to role-play.

Which entry is chosen, when a contract lists several, is decided by **the contract** — the
entry whose `contract_keys` declares both frozen keys (D3) — **not by position**. Ambiguity,
or any unparseable entry, is the third state, whole-or-nothing, as `scaffold_ignore_files()`
already is. A bare string entry is accepted alongside D3's object form, which is the same
string-or-object tolerance we are asking the desktop to add to `parseLayout`, so either host
can ship first.

**Reported, not repaired, and it is a recurrence rather than a new fault.** The claim that
this was "verified against eight mutated `layout.json` copies" describes scratch work that is
not in the tree; the tristate and the `contract_keys` selection were re-derived here by
reading the function. In the course of that, a **third site was found asserting the note
Bucket 19j established does not exist**: `desktop_state_file()`'s own docstring says of
`contract_exclude_patterns` and `is_secret_filename` that *"both carry a note saying not to
harmonise them"*. `grep -n "harmonis" docs/local_agent_kit/tools/kit.py` returns exactly one
line — that sentence. The two functions still do not name each other. **Not fixed here** (this
document's writer does not own `kit.py`), and worth more than the missing comment itself: the
absent note has now been asserted as present by two independent writers, which is what an
unfalsifiable claim about a protection looks like when it propagates.

**20d — `main()` now catches `(OSError, json.JSONDecodeError)`**, closing Bucket 10's
prediction that the narrow `KitError`-only handler "will bite again". `URLError` and
`HTTPError` are `OSError` subclasses, so one clause covers the network and filesystem
families, and a JSON file that will not parse is the other thing an ordinary user can hand
this tool. Nothing broader is caught, deliberately: a traceback for a genuine bug is
information, a traceback for a refused connection is noise the user cannot act on.

**Proven load-bearing on a genuinely reachable path, re-run here.** `chmod 000` on an agent's
`scripts/`, then `kit.py export`: with the clause, one line —
`error: [Errno 13] Permission denied: …/ag/scripts/README.md`. With the clause reverted (a
scratch copy narrowing it to `except (RuntimeError,)`), **22 lines of traceback**, re-counted
at this point of use rather than quoted forward.

**20e — a defect in landed work, reported not fixed.** That same probe shows an **unhandled
`PermissionError` on a directory the tool cannot traverse**, which the Phase 4/5 round's
file-level `unreadable[]` handling does not cover — that handling lives in
`hash_export_files`/`cmd_export` and fires on files that fail to *read*, while this one fires
on a `stat` of a path inside a directory that cannot be *entered*.

**The locus is wider than it was reported, and the correction makes it worse rather than
better.** It was handed over as "`cmd_export` has an unhandled `PermissionError`". The raise
site is `_validate_files` (`kit.py:2026`, `if not (agent_dir / relative).is_file()`), reached
through `validate_agent` — so `kit.py validate` fails the same way, verified by running it.
The export walk itself is already defended (`collect_export_files`'s `os.scandir` swallows
`OSError` per-directory, `_excluded_for_report` uses `os.walk`, which ignores errors by
default), which is precisely why the failure surfaces from the validator instead. Flag against
Phase 12 or a later fix round, **as a `validate` defect that `export` inherits**, not as an
export defect.

**20f — assertions chosen over filters, with the reasoning, all four verified in
`_stream_chat_response` and `_read_desktop_connection`.** A response line that is not UTF-8,
not JSON, or not a JSON object **fails the run** rather than being skipped — a skipped line
prints a shorter answer that still looks complete. A stream in which **no** line carried a
recognised text key fails loudly, naming the keys it did see: without that alarm, a desktop
naming its key `message` produces a silent empty success, indistinguishable from an agent that
had nothing to say. And a `chat_path` that is present but unusable **refuses** rather than
defaulting, because a 200 from the wrong endpoint is a failure nobody notices. (`null` is read
as absent, because that is how a writer says "unset" in JSON.) The same shape as the three
earlier instances the "a filter used where an assertion belongs" section collects.

**20g — two self-found issues in new code, and they resolved in opposite directions.**

- The `seen_keys` cap (`SEEN_KEY_LIMIT`) had no marker: a filter presenting a truncated key
  list as complete, inside the one diagnostic whose entire job is naming the key we failed to
  recognise. Now marked `(truncated)`, with a comment saying why.
- A guarded `sys.stdout.write("\n")` sits beside an unguarded `sys.stdout.flush()`. The
  guard-asymmetry heuristic fires on this and is **wrong here**: the newline is conditional
  because adding a second one to a stream that already ended in `\n` puts a blank line under
  every answer, and the flush is unconditional because a stream whose last chunk *did* end in
  `\n` still has to reach the terminal. The code stayed; a comment says they are deliberately
  not siblings. **Record this as a useful instance of the heuristic's false-positive rate** —
  the right response to a heuristic firing was a comment, not a change, and a document that
  only ever records the heuristic's hits will get it applied as if it never misses.

**20h — a near-miss kept and labelled.** The explicit `200 <= status < 300` check in
`cmd_chat` is unreachable while `build_opener` includes `HTTPErrorProcessor`, which raises
`HTTPError` for anything outside 2xx before that line runs — the
**documented-fail-safe-made-unreachable-by-something-upstream** shape, the same as the
`secret_file_rules` finding. It was **kept**, because the handler list is ours to change and
this file has been bitten once already by exactly that; and its comment marks it as a backstop
so nobody later reads it as the primary check. Keeping an unreachable guard is only defensible
*with* the label — unlabelled, it is the thing that made the earlier finding hard to see.

### Bucket 21 — the `PermissionError` fix round (closes Bucket 20e, and its fourth condition)

**Status: VERIFIED (recording run, re-derived against the working tree).** Everything below
was checked by this recorder against `docs/local_agent_kit/tools/kit.py` and by running the
tool, not taken from the implementation report. **No line numbers are cited**: `kit.py` was
under concurrent edit while this bucket was written, and the Standing checklist's rule about
line-level claims on a file under concurrent edit applies to the recorder as much as to a
brief. Findings are named by function. Grep outputs below are reproduced **verbatim**,
line numbers included, because a protection claim must carry the output rather than a
restatement of it — but those numbers are the volatile half: **re-run the grep, do not
trust the number.**

**Probe method, stated so it can be repeated.** `docs/local_agent_kit/` was copied to a
scratch directory and the copy's `kit.py` run with `backend/.venv/bin/python` (3.13 — the
system `python3` here is 3.9 and `kit.py` refuses below 3.10 with its `uv` message).
Non-vacuity: host and scratch `md5 -q .../tools/kit.py` both `1f8c2226c31a1146465b0ca5546bd28a`.
An agent was scaffolded with `kit.py new`, and `chmod` applied to it. The working tree was
never edited.

**21a — Bucket 20e's literal no longer reproduces as written, and that is the fix landing,
not the finding evaporating.** `chmod 000` on a scaffolded agent's `scripts/`, then
`kit.py validate .`:

```
  ERROR  scripts/ could not be read — nothing inside it could be checked. Fix its permissions and validate again.

FAILED — 1 error(s), 0 warning(s).
```

Exit 1, no traceback, no bare errno. `kit.py export` on the same folder refuses in the same
terms and says so of `--force` (21d). The directory-level fix is `collect_export_tree`, a
refusing `collect_export_files` wrapper, `unreadable_directories` + an `iter_files`
`on_error` parameter, a `validate_agent` pre-scan, and a `cmd_export` refusal.

**Weak evidence, called out rather than dressed up.** The brief offered "absent from
`git show HEAD:…kit.py`, present now" as the differential. It is literally true —
`collect_export_tree` returns nothing at HEAD and six hits now — but it proves almost
nothing, because HEAD's `kit.py` is **1486 lines against the working tree's 4149**: the
entire contract implementation is uncommitted, so *every* function this run added is "absent
from HEAD". The differential that carries weight is the behavioural one above, and the
reverted-copy ones in 21f.

**21b — the finding held one directory to the left, and the reason is the transferable
part.** The pre-scan was first scoped to the *travelling* subtree, on a comment justifying
that an unreadable `credentials/` "fails nothing". **That justification was true of `export`
and false of `validate`, in a function both reach.** `credentials/` and `app-data/` are
excluded from the export but *are* read by validate — `credentials/.env.example` is a
required file and the status file lives under `app-data/storage/` — and `Path.is_file()`
swallows ENOENT while **propagating EACCES**, so each `chmod 000` put the whole command on
20d's backstop as a bare `error: [Errno 13] Permission denied: …`. Two raise sites, not one:
`_validate_files` (the one 20e names) and `_validate_status_file` (which it does not).

Both now resolve through the pre-scan. Verified, one `chmod 000` per run:

```
  ERROR  credentials/ could not be read — nothing inside it could be checked. …   exit 1
  ERROR  app-data/ could not be read — nothing inside it could be checked. …      exit 1
```

The reasoning survives in the source, in the pre-scan's own comment — grep output rather
than a restatement of it, per the Standing checklist's protection rule:

```
$ grep -n 'fails nothing\|wrong command' docs/local_agent_kit/tools/kit.py
2546:    # is excluded from the export and so "fails nothing". That reasoning was
2547:    # about the wrong command. `credentials/.env.example` is a REQUIRED file and
```

**21c — the silent case outranks the crashes, and it is the cleanest instance in the run of
a filter standing where an assertion belongs.** `chmod 000` on `.claude/` made validate exit
**0**. Nothing required reads there, but `_validate_secrets` walks the whole folder hunting
stray key material — and `os.walk` yields **nothing** for a directory it cannot enter, with
no error and no marker. So the one check whose job is "there is no secret material in this
tree" **reported a clean bill of health over a subtree it never saw**. A crash is a bad
afternoon; this is a validator that says yes about something it did not look at. Now:

```
  ERROR  .claude/ could not be read — nothing inside it could be checked. …       exit 1
```

Same family as the "a filter used where an assertion belongs" section above, and belongs in
it: the walk *filtered out* what it could not read, and the caller read the empty result as
an answer.

**21d — the three-conditions ruling, and why they cannot be one list.** Verified in
`unreadable_directories`' docstring and by running each case:

1. **A file whose bytes will not read.** The file *is* in the export set and travels; per D7
   the digest folds it in as `UNREADABLE_MARKER`. Settled by the **hash**, over a finished
   file list.
2. **A directory the export walk cannot scan.** Files that should travel are absent from the
   set entirely. Settled by the **walk**, before a file list exists.
3. **A directory `validate` reads that the export never touches.** New in this round.

They cannot share one list: a single list could not be acted on until after the hash, which
is **later than the point at which (2) already makes the answer unusable** — and later than
`cmd_export`'s decision not to `mkdir`. Nor one message: "the hash does not describe the
bytes" and "files are missing from the upload with nothing saying so" are different facts.
**Three lists, three messages, one policy** — refuse, and `--force` waives none.

(3) cannot be folded into (2), and the reason is a working property rather than taste: (2)'s
walk never descends into an **excluded** directory, which is exactly what makes an unreadable
`credentials/` cost the export nothing. Verified directly — with `credentials/`, `app-data/`
and `.claude/` each `chmod 000` in turn, `content_hash` returned **the identical digest**
each time (`sha256:e9980a44db6c…`, 13 travelling files). Widening (2) to reach (3) would
break that.

The `--force` half, verified rather than read:

```
$ kit.py export . --to … --force        # with scripts/ at 000
error: these paths are inside the export but could not be read, so neither the set of files
that travels nor a content_hash over it describes a tree that was fully seen — and the files
under them would go missing from the upload with nothing saying so:
    - scripts/
Fix their permissions and export again. --force does not cover this: it waives validation
findings, not a tree this host could not read.
```

**21e — `content_hash` was the actual invariant violation, and its shape is the reason it
lasted.** It returned only the first element of `hash_export_files`, discarding the
unreadable list, **while its own docstring told callers to call `hash_export_files` instead
and inspect the second element**. That is a protection written as an instruction to the
reader least likely to go looking for it: someone reaching for the function whose name and
return type say *give me the number* is precisely the person who will not read past the
signature. Now structural, per D11's amendment — **unevaluable ⇒ refuse to emit a
`content_hash` at all, never emit one computed a different way**. Both halves verified by
running them:

```
content_hash refuses with KitError: these files are part of the export but could not be read, so a content_hash over them would describe a tree that was never fully read:
hash_export_files digest: sha256:9731d76ce6cfb … unreadable: ['cinna-agent.json']
```

So D7 parity is untouched: `hash_export_files` still folds in `UNREADABLE_MARKER` and still
returns the list; only the convenience wrapper refuses. Same shape as `collect_export_files`
over `collect_export_tree` — the policy wrapper, not a thinner spelling of the primitive.

**21f — a vacuity the implementer caught in its own probing, recorded as an instrument
failure because self-caught vacuity is the reliable signal this run keeps producing.** Its
first attempt `chmod 000`'d `README.md` — which does not travel — so the probe returned an
empty unreadable list: **a passing test of nothing.** Re-derived here: with the contract
patterns applied, `"README.md" in collect_export_files(...)` is `False`; the export set is
13 files and contains no `credentials/` or `app-data/` member. It re-ran against an actually
travelling file. This is the **exclude-list analogue of the stale-mirror trap** — an
instrument that reports success because it never touched the thing it claimed to test.

A second instance, same round, same shape: its first reverted-copy differential **threw
before writing**, so it measured the *unmodified* file and reported a comfortable
zero-crashes result. It re-ran with correct anchors **plus an assertion that the text had
actually changed**, and got seven-of-seven. That added assertion is the generalisable part:
a reverted-copy differential must prove the revert happened before it may report what the
revert caused.

**21g — evidence quality, and the backstop is deliberately not the thing doing the work.**
The fix was shown to stand **without** 20d's `(OSError, json.JSONDecodeError)` backstop, via
a scratch copy narrowing it, producing zero traceback lines; a reverted-copy differential
reproduces both original tracebacks. Confirmed in the real file that nothing narrowed leaked
in: `main()`'s handler is still `except (OSError, json.JSONDecodeError) as exc:` and is the
only such clause at top level.

Two probe failure modes came out of this verification and have been folded into the Standing
checklist's bidirectional-probe bullet rather than repeated here: an **absence probe returns
a false failure when the correcting text quotes the wording it retires** (hit by the
coordinator, with an accurate implementer report in hand), and **`grep -F` counts per line**,
so a multi-line needle yields a number pair that looks like a measurement and is not.

**21h — the fourth condition: an unreadable FILE during validate. RULED AND LANDED**
(supersedes this bucket's earlier TODO). Report-and-continue, at **check** granularity,
matching validate's existing treatment of a *missing* required file. Verified by running it:

```
  ERROR  docs/WORKFLOW_PROMPT.md could not be read (Permission denied) — the check that reads it was skipped. …
FAILED — 1 error(s), 2 warning(s).
```

**The ruling's stated premise was wrong, the implementer said so, and the correction is the
transferable half.** Missing and unreadable are the same class of fact **semantically** — the
content is unavailable — and **not structurally**, and the structural difference decides
where the handler can live. A missing file is knowable *before* the read, by a total
predicate (`is_file()`) every caller already consults, which is why report-and-continue is
free there. An unreadable file is knowable **only by attempting the read**, from inside
whichever helper attempted it, with no predicate to consult.

So the two read helpers could not deliver it, for two independent reasons:

- Returning `""` for a file that could not be read would report an unreadable
  `WORKFLOW_PROMPT.md` as an **empty** one. **The "fix" would have manufactured a plausible
  finding rather than dropping one** — it moves the reader from neutral to *wrong*, which is
  strictly worse than the bare errno it replaces. **The cleanest instance in this run of the
  filter-where-an-assertion-belongs family**, and it belongs in that section: every other
  instance loses information, this one invents it.
- Raising a different exception type out of helpers used all over the file would silently
  escape every `except OSError` already standing around them.

The handler therefore went to the seam where the check boundary already was — the
sub-validator dispatch — one edit, **both read helpers byte-unchanged**. Verified: `read_text`
is a single `return path.read_text(encoding="utf-8", errors="replace")` with no handler, and
`read_json` likewise propagates; neither carries a `try` around the read.

**The granularity limitation was weighed and DECLINED, and is recorded as a ruling rather
than an omission.** One ERROR per failing *check*, not per *file*: two unreadable files read
by the same check yield one ERROR naming the first, and the second appears on the next run.
Re-derived here rather than quoted — with the agent's `pyproject.toml`, `Makefile`,
`<agent>/docs/WORKFLOW_PROMPT.md` and `<agent>/docs/CLI_COMMANDS.yaml` all at `000`, validate reports
**three** ERRORs; `Makefile` is masked because `_validate_commands` reaches the YAML first.
Closing it means editing seven individual call sites for a diagnostic nicety, late in a run,
against a bound the ruling had set — and the user simply re-runs `validate`. The reason is
recorded because **an undocumented limitation invites a later editor to "fix" it without
knowing the trade was deliberate**; the source comment carries the real granularity and the
verified example, so the artefact says it too, not just this document.

**How the limitation was found is itself worth the line:** the implementer contradicted a
comment it had just written. It claimed "one pass names every permission", ran the
four-file case, got three, and corrected its own comment. Same self-caught shape as 21f.

**One read stays refuse-early, deliberately: the manifest.** `read_json(manifest_path)`
failing returns immediately rather than continuing, because every check below it is a
statement *about the manifest* — continuing would answer nine questions about a file nobody
read. The two branches beside it (not-JSON, not-an-object) already return for the same
reason.

**21i — a structural control, and it is the same move as one-sole-writer-of-the-manifest.**
`iter_files` gained an `on_error` parameter (default `None`, i.e. `os.walk`'s own
ignore-errors behaviour, so every existing caller is unchanged) so that **validate's scan
scope and validate's widest walk are one walker, not two**. A separate scanner written to
answer "which directories could I not enter?" would be a second definition of validate's
scope, free to drift the first time someone edited `SKIP_DIRS` on one of them. The parallel
is stated in the source, which is where it has to be:

```
$ grep -n "sole writer of the manifest" docs/local_agent_kit/tools/kit.py
1180:    function the sole writer of the manifest: a rule someone must remember
```

Full sentence, in `iter_files`' docstring: *"Same move as making one function the sole writer
of the manifest: a rule someone must remember ('keep the scan's scope equal to the walk's')
becomes a property of there being only one walk."* This is the general technique the run
keeps rediscovering — **enforcement moves out of the person and into the artefact** — and the
Standing checklist asks for the parallel to be stated wherever either is taught.

### Bucket 22 — the CHANGELOG round (Phase 10 step 3/4, run standalone as blocking)

**Status: VERIFIED (recording run, re-derived).** Phase 10's step 3 was filed as blocking and
not trimmable; it ran on its own. Everything below was checked against the tree by this
recorder.

**22a — all three legs of Bucket 16i.3's finding held, and the third leg is the one that
decided how the CHANGELOG was written.**

- `CHANGELOG.md` is genuinely a contract tarball member. Verified in
  `backend/app/services/cli/local_agent_kit_service.py`:
  `CONTRACT_MEMBERS = frozenset({INDEX_MEMBER, LAYOUT_MEMBER, CONTRACT_VERSION_MEMBER, CHANGELOG_MEMBER})`,
  with `CHANGELOG_MEMBER = "CHANGELOG.md"`. So a false statement in it is one **we package and
  hand to another team**, not a stale internal note.
- Both false statements were present.
- They were **falsified by Phase 7 rather than always wrong.** `SUPPORTED_SCHEMA_VERSION` is
  absent from `docs/local_agent_kit/tools/kit.py` now and present at HEAD (five hits, at
  `:57`, `:360`, `:363`, `:365`, `:367`). That distinction is why the old text was preserved
  **in the past tense under a "Pre-contract" heading** rather than deleted: there is a real
  population of folders in the field that was scaffolded under exactly those rules, and their
  owners need the entry that describes them.

**22b — the §4 Compatibility table is byte-identical to the desktop's authored section.**
Re-derived by extracting both and diffing:

```
$ diff ours_compat.txt theirs_compat.txt && echo IDENTICAL
IDENTICAL
$ md5 -q ours_compat.txt theirs_compat.txt
bb9dd150f34685592c9b8cf7a1b50a6d
bb9dd150f34685592c9b8cf7a1b50a6d
```

Extent, counted at this point of use: **8 lines** — the `## Compatibility` heading, a blank
line, and a 6-line table (header row, separator, four data rows). Ours sits at the end of
`docs/local_agent_kit/CHANGELOG.md`; theirs in
`/Users/evgenyl/dev/ml-llm/cinna-desktop/resources/cinna-kit-contract/CHANGELOG.md`. Three
implementations key off this table, and `schema/cinna-agent.schema.json`'s identity
`$comment` says so in as many words — "if you change one, change all three" — so byte
identity is the property, not paraphrase.

**22c — a trap avoided, recorded for its shape rather than for the incident.** The 1.0.0
entry describes the new scaffold tokens by naming them **bare** — *"the set is `NAME`,
`SLUG`, `DESCRIPTION`, `ID`, `CREATED_AT`, `CONTRACT_VERSION` and `KIT_VERSION`, each written
in the templates as its name between double braces"* — rather than writing the braced
literals. The reason is that `CHANGELOG.md` is **itself a rendered tarball member**, so a
literal `{{UPPER_SNAKE}}` in it would be caught by the very scanner that guards against
unrendered placeholders (`test_no_unrendered_placeholder_survives`, whose
`_UNRENDERED_TOKEN = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")` scans every tarball member).
**A document that describes a mechanism can be consumed by it.** Verified: the file now
contains exactly **one** `{{…}}` token —

```
$ grep -o "{{[A-Za-z_]*}}" docs/local_agent_kit/CHANGELOG.md | sort | uniq -c
   1 {{KIT_VERSION}}
```

— and that one is legitimate, because `KIT_VERSION` is a token the *server* renders
(`_VERSION_TOKEN` in `local_agent_kit_service.py`), so it never survives into a shipped tree.

**22d — a defect in landed work, confirmed independently by this recorder, and the ORDERING
HAZARD is the headline rather than the defect.** Phase 6's uppercase scaffold tokens break
tests in `backend/tests/api/cli/test_local_agent_kit.py`:

- `test_no_unrendered_placeholder_survives` — its regex matches `{{UPPER_SNAKE}}`, and the
  templates now carry six such tokens the server does not render. Re-derived over
  `docs/local_agent_kit/templates/`: `{{NAME}}` ×11, `{{SLUG}}` ×3, `{{DESCRIPTION}}`,
  `{{ID}}`, `{{CREATED_AT}}`, `{{CONTRACT_VERSION}}` ×1 each (plus `{{KIT_VERSION}}` ×1,
  which *is* rendered). The server-side token set is `PLATFORM_URL`, `KIT_BASE_URL`,
  `INSTANCE_NAME`, `KIT_VERSION` and the URL/CLI values beside them — none of the six.
  **Its own comment still claims the scaffold tokens are lowercase**, which is the part a
  later reader will believe: *"Placeholders are `{{UPPER_SNAKE}}`. The lowercase `{{name}}` /
  `{{slug}}` tokens in `templates/agent/**` are deliberately not rendered here."*
- `test_scaffold_placeholders_are_left_for_kit_py` — the complement, asserting
  `"{{name}}" in templates/agent/AGENTS.md`. Re-derived: `grep -c "{{name}}"` on that file is
  **0**, and a repo-wide scan of `docs/local_agent_kit/templates/` finds **no lowercase
  `{{…}}` token at all**.

**The hazard, stated as the failure rather than the rule.** Both breakages are **invisible in
a container run against the stale mirror**, because the mirror still holds the old tokens —
re-derived at this point of use:

```
$ grep -rhoE "\{\{[A-Za-z][A-Za-z0-9_]*\}\}" …/platform-knowledge-env/…/local-kit/templates/ | sort | uniq -c
   2 {{KIT_VERSION}}
  11 {{name}}
   3 {{slug}}
```

So anyone running the suite **before** the sync gets a green; it flips red **after** the sync;
and the natural conclusion is that **the sync broke it**. Someone reaching that conclusion
reverts the sync — the one action that restores the green **and** leaves the real defect in
place. Phase 11's own sequencing rule ("sync LAST") therefore *creates* this trap unless the
hazard is written down, which is why it is here and not only in Phase 12.

**22e — the enumeration gap, and it is a pattern rather than two incidents.** Phase 12 item 14
named `test_scaffold_placeholders_are_left_for_kit_py` and the `PLATFORM_TOKEN_RE` heuristic,
and **not** `test_no_unrendered_placeholder_survives`. Nobody was careless: that item was
written before Phase 6 existed in its final form. **The item has been amended in place** by
this recorder, and the general rule — a phase that enumerates specific artefacts goes stale
exactly the way a quoted figure does — has been folded into the Standing checklist's
stale-figure bullet rather than added as a second entry.

Then a **fourth** affected test turned up that no phase list names at all:
`test_version_payload_matches_the_index` asserts
`payload["schema_version"] == index["schema_version"]` — precisely the coupling D17 dissolves,
and it now raises on **both** sides. That is the **second independent breakage in that one
file**, and both are invisible before the mirror is synced. **Two is a pattern, not a
coincidence: whoever picks up Phase 12 should expect a THIRD rather than assume two is the
count.** An enumeration presented as complete is the failure mode this run keeps meeting; this
record deliberately under-claims rather than handing someone a list they will trust.

A third observation on the same heuristic, worth a line because it fails *silently*:
`PLATFORM_TOKEN_RE = re.compile(r"^\{\{[A-Z][A-Z0-9_]*\}\}$")` in
`backend/tests/unit/test_local_kit_tool.py` **exempts** UPPER tokens from its
leftover-scaffold-token check, on the pre-Phase-6 premise that UPPER means "platform-rendered,
legitimately literal in a checkout". After Phase 6 the scaffold tokens are UPPER too, so an
**unsubstituted `{{NAME}}` in a scaffolded agent would now pass that check** — the heuristic
goes vacuous exactly where it was meant to bite. It does not fail today (`kit.py new`
substitutes them all), which is the problem: a guard that stops guarding without failing.

**Not D17's, and the distinction was worth proving before anything was touched.** The
pre-existing `KeyError: 'schema_version'` in `test_new_scaffolds_and_validate_exits_zero`
(`assert manifest["schema_version"] == 1`) reads the **manifest's** field — a Phase 7
consequence of the agent template dropping the key, already recorded under Bucket 14's four
survivors. **The manifest's `schema_version` is live, load-bearing and stays.** Only
`kit.json`'s went.

**22f — D17: `kit.json`'s vestigial `schema_version` is REMOVED. This SUPERSEDES the note
that recorded it as known-vestigial-and-deliberately-left, which was this run's
recommendation and was overruled.** The reversal is recorded rather than edited away, because
the reasoning for the overrule is the useful part.

The recommendation was: `kit.json` retains `"schema_version": 1` beside `contract_version`,
nothing reads it, leave it as out of plan scope and record it as known-vestigial so the
desktop team does not meet it as a stray. The ruling went the other way, and the ruling is
better:

- **This feature exists to eliminate the second number that decides nothing** — D11's and
  D13's argument, and the reason `contract_version` exists at all. `kit.json` is half the
  identity pair the desktop reads a contract tree by, so leaving the field there ships the
  exact artefact this run removed everywhere else.
- It is inert **today**; the failure mode is **the next tool treating it as meaningful**, with
  nothing anywhere to prevent that.
- The desktop's own authored `kit.json` carries **no** `schema_version` — verified, its keys
  are `name`, `title`, `description`, `contract_version`, `schema`, `layout`, `templates`,
  `refresh` — so this moves toward their file rather than away from it.
- The kit is unreleased: the same timing argument R4 used.

**The second site was mis-characterised in the brief, and the correction strengthens D17
rather than weakening it.** It was described as "the matching literal in the backend's
fallback payload". It is **not a fallback**: it is `_version_payload`, the live envelope of
`GET /agent-start/version` — the endpoint `kit.py refresh` polls. **So D17 changed a public
JSON response shape**, which is a materially bigger act than editing a fallback, and the
answer-back must say the second thing rather than the first. It strengthens the decision
because a number that decides nothing, sitting on the endpoint `refresh` actually polls, is a
**sharper** instance of "the next tool treats it as meaningful" than the same number on a path
nobody reaches. (The general rule this yielded — a characterisation quoted forward is the
stale-figure mechanism operating on prose — has been folded into the Standing checklist's
stale-figure bullet.)

**Why removing a field from an unauthenticated, every-instance endpoint is nonetheless safe,
and this argument is the reusable part.** "No reader in either repo" cannot answer the real
question, which is whether a consumer exists that we cannot see. What makes it safe is **the
nature of the field: it is a synthesised constant.** It has never varied and never could, so a
hypothetical external consumer reading it learns nothing and nothing downstream can be
computing anything from it. Generalised: **a constant field is exactly the one whose removal
breaks only code that reads it without using it.**

**Verified state, re-derived at this point of use.** Both sites moved atomically — a fallback
disagreeing with the shipped file would be worse than either state alone:

```
$ grep -n schema_version docs/local_agent_kit/kit.json      → (no match)
$ grep -n schema_version backend/app/services/cli/local_agent_kit_service.py
260:        There is deliberately **no** ``schema_version`` here. It used to be
265:        ``schema_version`` is a different, still-live legacy marker and is not
```

The removal left a protection behind it, which is the right shape for a deliberately absent
field — `_version_payload`'s docstring: *"Do not re-add it 'for parity with `kit.json`' —
`kit.json` no longer carries one either."*

**"Nothing reads it", verified in both repositories.** In this repo, no reader remained beyond
the two sites removed. In the read-only desktop tree, every `schema_version` reference
concerns the **manifest**, never `kit.json`: `src/shared/kit/manifest.ts` types
`schema_version?: number` on the manifest; `src/main/kit/validator.ts` uses it for the legacy
gate and the `manifest.schema_version.legacy` warning; `src/shared/localAgents.ts`,
`localAgentService.ts` and the `validator`/`scanner`/`localAgentService` test files all
reference the manifest's; and `resources/cinna-kit-contract/CHANGELOG.md` discusses it as the
retired manifest gate. Their `kit.json` never had one.

**The desktop must be TOLD the field is gone**, deliberately, rather than left to notice a
field that quietly vanished from an endpoint. That is an answer-back line, not a plan note.

**22g — two earlier buckets were STALE on the template's `publications` key, and the
supersession turned out to be already half-present. Recorded rather than corrected silently.**
Bucket 16i.1 ("`templates/agent/cinna-agent.json:25` still ships `"publications": []`") and
Bucket 17 HIGH 1 (same claim, as its opening sentence) were **true when written and are false
now**. Re-derived by parsing the template rather than grepping it — the key list is
`contract_version, id, kit_version, created_at, name, slug, description, example_prompts,
router_trigger_prompt, prompts, runtime, status_refresh_command, credentials, schedules,
handovers, features`: **neither `cloud` nor `publications` is present.** And `cmd_new` now
**raises** if either reappears in the kit's own template —
`ledger_keys = [key for key in ("publications", "cloud") if key in manifest]` → `KitError`
naming `TEMPLATE_DIR` and telling the user it is a defect in the kit, not in anything they
did.

**Contradiction with the brief that produced this bucket, recorded as the checklist requires.**
The brief asked for these to be flagged as unsuperseded. They are **not**: Bucket 19a already
records the removal in full, and the answer-back-facing section already re-derives our half at
its own point of use (*"`grep -n publications docs/local_agent_kit/templates/agent/cinna-agent.json`
now returns nothing"*). What was genuinely missing is a **pointer at 16i.1 and at 17 HIGH 1
themselves**: an answer-back writer reading either in isolation would take it as current. Both
now carry a dated supersede note, added in place — neither finding was deleted or softened,
because each was true as of its own bucket and the pre-fix divergence is the evidence trail
for the ask.

**The desktop's half is still live, verified now:**
`resources/cinna-kit-contract/templates/agent/cinna-agent.json:25` still reads
`"publications": []`. The remedy remains the D5 wholesale re-bundle already on the
required-changes list — **do not add it there a second time.**

### Bucket 23 — the D8 `~/Documents/CinnaAgents` sweep

**Status: VERIFIED (recording run, re-derived).**

**23a — the sweep, counted at this point of use rather than quoted forward.**
`~/Documents/MyAgents` → `~/Documents/CinnaAgents`, and `MyAgents/` → `CinnaAgents/` in the
one frontend literal that carries no `~/Documents` prefix. **9 files, 14 occurrences on 13
lines** (`START.md`'s `mkdir -p` line carries two):

| File | occurrences |
|---|---|
| `docs/local_agent_kit/START.md` | 5 |
| `docs/local_agent_kit/assistants/codex.md` | 1 |
| `docs/local_agent_kit/guides/05-schedules.md` | 1 |
| `docs/local_agent_kit/guides/11-go-cloud.md` | 1 |
| `backend/app/services/cli/local_agent_kit_service.py` | 1 |
| `frontend/src/components/Onboarding/GettingStartedModal.tsx` | 1 |
| `.cinna-core-kit/scripts/check_docs_references.py` | 1 |
| `docs/application/local_agent_kit/local_agent_kit.md` | 2 |
| `docs/application/local_agent_kit/local_agent_kit_tech.md` | 1 |

**23b — the residual, with every hit accounted for.** Repo-wide `MyAgents` is **20
occurrences across 8 files**, and all 20 fall into one of three deliberate exclusions:

- **The generated `platform-knowledge-env` mirror — 10.** `local-kit/START.md` 5,
  `local-kit/assistants/codex.md` 1, `local-kit/guides/11-go-cloud.md` 1,
  `local-kit/guides/05-schedules.md` 1, and
  `platform/application/local_agent_kit/local_agent_kit.md` 2. Regenerated by
  `make sync-platform-knowledge`, **never hand-edited** — editing it would be editing the
  output rather than the source, and the next sync would silently revert it.
- **The historical draft plan — 6.** `docs/drafts/local-agent-kit_plan.md`, left alone by
  Phase 10's own instruction: it is an artefact of what was planned, not a statement of what
  is.
- **The planning documents themselves — 4.** This plan (3, in the A1 row and Phase 10 step 1,
  where the old string is the subject) and
  `docs/plans/local_agent_kit_desktop_contract_requirements.md` (1).

So: **no unaccounted residual.** Post-sweep `CinnaAgents` is 20 occurrences across 11 files —
the 9 above plus the two planning documents.

**23c — a POSITIVE instrument note, recorded because a document that only records failures
gets its own rules applied selectively.** **Every one of the plan's D8 line citations was
accurate when independently located.** All thirteen lines named in the A1 row and Phase 10's
file list resolved exactly: `START.md:20,24,28,73`, `assistants/codex.md:18`,
`guides/11-go-cloud.md:66`, `guides/05-schedules.md:111`, `local_agent_kit_service.py:7`,
`GettingStartedModal.tsx:224`, `check_docs_references.py:285`,
`local_agent_kit.md:83,205`, `local_agent_kit_tech.md:338`. Cross-checked in **both**
directions: the pre-sweep line numbers were confirmed against the un-synced mirror (which is
a verbatim copy of the pre-sweep tree, and so a free snapshot of the state the citations were
written against), and the post-sweep numbers are identical because every edit was
same-line.

This run's standing assumption is that line citations go stale; **this is the counter-instance,
and saying so is what keeps the rule a rule rather than a superstition.** The distinguishing
feature is visible and reusable: those citations named files **nobody was editing** — prose
and templates, not `kit.py`. The stale-citation rule's real subject is *files under concurrent
or subsequent edit*, and a document that never records the citations that held would grow into
a blanket distrust of all of them, which is both wrong and unactionable.

**23d — a wording defect in the plan itself, fixed in place.** Phase 10 step 1 read "every
`~/Documents/MyAgents` → `~/Documents/CinnaAgents`, **in all of the above**", but most files in
that list contain no occurrence and belong to other steps — the step's true scope is the nine
files in 23a. Reworded to name the sweep's own scope and to say the list above is a phase
inventory rather than a replacement target.

**23e — a gap in Phase 11's checklist, and it is the valuable item in this bucket.**
`docs/application/local_agent_kit/local_agent_kit.md` reaches the knowledge env through
`sync_docs()`, landing at
`backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/platform/application/local_agent_kit/local_agent_kit.md`
— **not** through `sync_local_agent_kit()` and **not** under `local-kit/`. Phase 11 step 1
named only the latter, so its verification step would have sent nobody to look at the other
mirror. Two of the twenty residual `MyAgents` occurrences are sitting in exactly that
un-named tree, which is how this surfaced.

**It is a VERIFICATION gap, not a live one** — verified rather than assumed, by reading the
script's entry point. `main()` in `.cinna-core-kit/scripts/sync_platform_knowledge.py` runs all
three steps in one invocation — `[1/3]` `sync_docs()`, `[2/3]` `sync_api_reference()`, `[3/3]`
`sync_local_agent_kit()` — so the **single** `make sync-platform-knowledge`
(`python3 .cinna-core-kit/scripts/sync_platform_knowledge.py`) refreshes both trees. Nothing is
missing from the sync; what was missing was the instruction to look at both of its outputs.
**Phase 11 step 1 has been amended in place** to require confirming both mirrors.

Also recorded, because it will otherwise read as a third gap: **`local_agent_kit_tech.md` is
never mirrored at all, by design.** `sync_docs()` skips any file whose name contains `_tech`
(`if "_tech" in md_file.name: continue`) — the knowledge env carries business-logic docs, not
implementation detail. Its D8 occurrence was still swept, because the repo file is the one a
developer reads; it simply never reaches a mirror to be checked.

### Bucket 24 — two late findings: the `{{KIT_VERSION}}` asymmetry, and a false rationale in correct code

**Status: VERIFIED (recording run).** Both were handed over as claims and are recorded here
only after being checked against the artefacts.

**24a — `{{KIT_VERSION}}` is filled by the SERVER, not by the scaffolder, and the asymmetry is
undocumented on both sides. It is a live defect in the desktop's code and is on the
required-changes list as one.** Verified:

- `docs/local_agent_kit/templates/agent/cinna-agent.json` carries
  `"kit_version": "{{KIT_VERSION}}"`.
- `LocalAgentKitService` substitutes that token across **every rendered member** before
  delivery — `_VERSION_TOKEN = "{{KIT_VERSION}}"`, applied over the whole staged dict — so in
  any kit or contract tarball a client downloads, the token is **already resolved**.
- The desktop's `MANIFEST_TOKENS` (`src/shared/kit/manifest.ts`) enumerates seven tokens —
  `SLUG`, `NAME`, `DESCRIPTION`, `ID`, `CONTRACT_VERSION`, `KIT_VERSION`, `CREATED_AT` — and
  so will likewise find nothing to fill for the last of them.

**Six of the seven scaffold tokens are filled by the scaffolder; the seventh is filled by the
server before anyone sees it**, and nothing anywhere says so. Neither host has noticed because
the value comes out correct either way: our `cmd_new` substitution also finds nothing there,
harmlessly. **It is latent on their side rather than firing** —
`grep -rn MANIFEST_TOKENS src/` in their tree returns its own declaration and the derived
`ManifestToken` type alias and nothing else, the same no-production-consumer state Bucket 17
recorded — which is exactly why it is worth sending now: cheap to fix before it has a caller.
Phrase it to them as *"here is something in your code that will not do what it expects"*, not
as *"note this difference"*. The contract-documentation half goes into the kit's `README.md`
Placeholders section, owned by that file's writer; §0 carries the answer-back half only.

**24b — a false rationale attached to correct code, and it is the purest instance of the
wrong-explanation shape.** `contract_version()`'s docstring in `kit.py` justified its
`CONTRACT_VERSION` fallback on the grounds that a contract tarball "ships no `kit.json`".
**False:** `INDEX_MEMBER = "kit.json"` and `CONTRACT_MEMBERS` includes it
(`backend/app/services/cli/local_agent_kit_service.py`), and D2 states it explicitly. The
function's *behaviour* was right throughout — so nothing would ever have failed, and the only
casualty was the next reader's belief about what the contract contains, including a reader
deciding what their own client must fetch.

**Fixed by `kit.py`'s owner, and fixed in the shape the class calls for** — the true, narrower
scope of the fallback stated, and the false justification forbidden **by name** so it cannot
be reintroduced by someone reasoning from scratch. Grep output rather than a restatement:

```
$ grep -n "Do not justify the fallback" docs/local_agent_kit/tools/kit.py
790:    this one. **Do not justify the fallback below by claiming the tarball lacks
```

Its neighbourhood now reads: *"It travels in the contract tarball as well as in the full kit
— `INDEX_MEMBER` is one of `CONTRACT_MEMBERS` … The file the contract tarball genuinely omits
is `VERSION`, which carries the KIT version and not this one."* The narrower thing the
fallback actually covers is a `.cinna-kit/` whose `kit.json` is absent or lacks a usable
`contract_version` — a hand-assembled or half-refreshed tree, never one the platform served,
since the serving path 503s on exactly those states.

**Recorded as a shape, not as an incident.** The generalisation — *a wrong explanation is
worse than a missing one, because uncertainty sends a reader to check and a false
explanation stops them looking* — is written into the answer-back shapes section alongside its
first instance (the falsely-asserted cross-reference note, Buckets 19j/20c), and the Standing
checklist's protection entry has been **sharpened in place** to cover any explanation in a
report *or in the source*, rather than protections in reports only. Two entries saying it
would be how the rule stops being read.

### Bucket 25 — the token-list propagation, its root, and two courtesy items

**Status: VERIFIED (recording run).** Every claim below was checked against the tree; the
desktop half was checked read-only against their source.

**25a — the drift ran the wrong way round, and that is the first recordable thing.**
`SCAFFOLD_TOKENS` in `kit.py` is the authority and **was right all along** — the comment above
it already stated the provenance mechanism correctly. **The shipped documentation had drifted
away from a source comment that was accurate**, which inverts the usual assumption that code
drifts from docs. Worth naming for that reason alone: a doc-vs-code disagreement gets triaged
by an unstated prior that the code moved, and here that prior would have sent the fix to the
wrong file.

The authority, verified in place — note that it does **not** classify, and says so:

```
$ grep -n "settles the question is PROVENANCE\|in both sets on purpose\|A list cannot" docs/local_agent_kit/tools/kit.py
140:# apart by shape — and this tuple does not tell them apart either. A list cannot
142:# settles the question is PROVENANCE, and that is the next sentence.
143:# `KIT_VERSION` is in both sets on purpose: the platform fills it when it renders
```

The surrounding comment, read in full rather than grepped, says the tuple is UPPER_SNAKE like
the platform-render tokens so **the two classes cannot be told apart by shape — and the tuple
does not tell them apart either**: *"A list cannot classify a member it contains, and this one
contains `KIT_VERSION`. What settles the question is PROVENANCE."* The tuple itself is
`SLUG, NAME, DESCRIPTION, ID, CONTRACT_VERSION, KIT_VERSION, CREATED_AT`, one per line.

**25b — six enumeration sites in this repo, two wrong, both fixed; and the root is the plan
document.** The sweep found six places restating the set. Two were wrong —
`templates/agent/README.md` and the kit's own `README.md` — and both now carry the provenance
rule. Verified: each says the seven, then separates them by origin, and each names the tokens
**bare** (see 25d for why they must).

**The self-caught half, and it is the reusable observation:** the second copy of the error had
been introduced by **the very agent that later caught it**. That is the same self-caught shape
as §0 Bucket 21f's two instrument vacuities, and it is the reliable signal this run keeps
producing — an author re-deriving its own claim is the mechanism that works, where an author
re-reading its own claim is not.

**The root: Phase 6 step 1 of this plan enumerated the seven tokens as the desktop's
`MANIFEST_TOKENS` with no provenance caveat.** Every downstream copy inherited a set that is
**correct as a set and silent on the one member that behaves differently**. The caveat has been
added **at the source** by this recorder — a downstream fix without it leaves the next copy free
to drift the same way, which is the same argument as one-sole-writer-of-the-manifest and
one-walker-not-two, applied to prose.

**25c — the desktop's `MANIFEST_TOKENS` is the SEVENTH site, and it goes to the answer-back as
OUR defect with THEIR symptom. Lead with our half; do not open with their code.** It sat
outside the repo-wide sweep because it is outside the repo, and it is precisely where our wrong
list landed in someone else's source. The sendable framing, ruled:

> We published a list that classifies `KIT_VERSION` as a scaffold token. Your `MANIFEST_TOKENS`
> enumerates it because our contract documentation told you to — **your enumeration is the
> correct implementation of the list we gave you.** What we failed to publish is the caveat: the
> platform substitutes `KIT_VERSION` across every file when it renders a kit for download, so in
> any kit you actually download it is already a value and your seventh substitution will find
> nothing to do. Six of the seven are what is really waiting.

Verified state on their side, read-only: `MANIFEST_TOKENS` (`src/shared/kit/manifest.ts`) lists
the seven, and `grep -rn MANIFEST_TOKENS src/` returns only its own declaration and the derived
`ManifestToken` type alias — **still no production consumer**, the same state Bucket 17
recorded. That is what makes this cheap to send now: it is a bug waiting in their scaffolder,
not one firing in it.

**25d — the constraint that shaped all of this, stated in prose for the first time.** *A
rendered member cannot contain a literal `{{TOKEN}}`: the server substitutes it before
delivery, so any member that needs to **discuss** a token must name it **bare**.* It has bitten
three times in one day — `CHAT`-era `CHANGELOG.md` (§0 Bucket 22c), and both README fixes above
— and it was never written down. It **is** already encoded in the source, which is why nothing
ever broke:

```
$ grep -n "Built rather than written out" -A 3 docs/local_agent_kit/tools/kit.py
414:    Built rather than written out, because `kit.py` is itself served through the
415-    platform renderer: a literal double-brace UPPER_SNAKE sequence in this file
416-    is indistinguishable from a platform placeholder, and would be substituted
417-    (or flagged as an unrendered one) on its way to the user.
```

**The second-order effect is the part worth sending.** The constraint silently forced the
original Placeholders paragraph into an awkward form that *described* its tokens rather than
writing them — **and that awkwardness is what made the wrong list easy to miss.** A constraint
that deforms prose degrades the reviewability of that prose, and the defect hides in the
deformation. The answer-back shapes section carries the rule **and** this consequence; a team
told only the rule writes the same awkward paragraph and inherits the same blind spot.

**25e — `kit.py list`: a COURTESY item for the answer-back, and it must not be filed as a
required change or written in the register of 25c.** Phase 9's changes to `list` output are
ruled **Changed**, not Breaking: `list` output has never been a declared contract surface, and
the legacy flat `Cloud/` layout still works and still lists. That ruling stands and the
CHANGELOG entry reflects it.

Verified against the source, both facts:

- The table gained a **fifth column**: `_print_table(["SLUG", "NAME", "RUNGS", "CLOUD", "DESKTOP"], rows)`.
- The `CLOUD` cell now answers `yes` for a folder stamped with a `publications` ledger where it
  previously answered `no`. `is_published` "reads the ledger FIRST and the deprecated `cloud`
  stamp second" — the same question, asked of the file R4 moved the answer into. It is also the
  **one** reader of that question, shared with `_rungs_present`'s `go_cloud` rung, so the `RUNGS`
  and `CLOUD` cells of a row cannot contradict each other.

One sentence to them, framed as: **not a contract surface — but if you parse it positionally,
it moved.**

**Keep this and 25c apart in tone, deliberately, and the reason is not politeness.** 25c is an
**apology**: we published a wrong list and caused a bug in their code. 25e is a **heads-up**: we
changed something we never promised to keep stable, and are telling them anyway because a
sentence from us is cheaper than an afternoon of their debugging. **Blurring the two devalues
the apology** — an apology filed among routine notices reads as a routine notice, and the one
item that needed to land as an admission stops landing as one.

**25f — the owed-list was overstated, and the cause is known.** Phase 9's four steps were found
**already implemented** when the phase was dispatched. Not a mystery: the team lead has claimed
it — the handover's list of outstanding work was written **from their own reports rather than
from the tree**, across a session boundary, after several items had landed since they last
looked. **The list was true when written; the falsity entered when the tree moved underneath
it** — the stale-figure mechanism with *position* as its subject. Recorded here, and the rule
it yields (**verify each phase's premises against the tree before dispatching; never start from
"this is owed"**) has been **sharpened into the Standing checklist's existing position-half
entry in place**, not added as a second rule.

**No authorship claim is made about who implemented those steps, and the abstention is
deliberate.** The whole feature is uncommitted, so git carries no authorship evidence, and
**mtimes are not authorship evidence** — several agents share this tree and a recent mtime looks
exactly like a fingerprint. *"This was already done; the list was stale"* is both true and more
useful than any attribution: the attribution answers a question nobody needs answered, the
staleness answers the one that recurs.

**What the phase did instead is the argument for the rule.** Its agent verified by execution
rather than re-implementing, found the steps present, found a **real defect in the landed code**,
and fixed that. Starting from "this is owed" would have produced a re-implementation of working
code and left the defect standing.

### Bucket 26 — a token leak in the plan's own instruction, a consumer we broke silently, and four smaller items

**Status: VERIFIED (recording run).** Every figure below is re-derived at its point of use;
every protection-shaped claim carries its grep output rather than a restatement of it. Where a
handed-over claim did not survive checking, the correction is stated rather than the claim.

**26a — the gitignore pattern that cannot match the path it was written for, and it is in THIS
document's instruction rather than in the shipped file.** Phase 10's template file list read:

```
- `docs/local_agent_kit/templates/root/gitignore` (add `Cloud/*/.cinna/`, and
  `.cinna-kit/.last_refresh_check`)
```

**A `*` segment never matches an empty one**, so `Cloud/*/.cinna/` cannot match
`Cloud/.cinna/`. Reproduced independently in a scratch repo outside this tree
(`/tmp`, `git init`, that single pattern):

```
$ git check-ignore -v Cloud/.cinna/account.json
NOT IGNORED: Cloud/.cinna/account.json
$ git check-ignore -v Cloud/host.example.io/.cinna/account.json
.gitignore:1:Cloud/*/.cinna/	Cloud/host.example.io/.cinna/account.json
$ git status --porcelain --untracked-files=all
?? .gitignore
?? Cloud/.cinna/account.json
```

**What that would have shipped.** `.cinna/account.json` holds an account bearer token — it is
why cinna-cli writes it `0600` (`_login_new_account` → `AccountConfig` materialisation,
`src/cinna/account.py`). Phase 9 deliberately keeps the legacy flat layout working, and the
CHANGELOG files that decision under `### Changed` rather than `### Breaking`: *"An existing
flat `Cloud/` keeps working: it is still recognised, still listed, and lists alongside
per-instance workspaces in the same run, so nothing has to be moved."* So a scaffold following
the plan's line would have ignored the token in per-instance workshops **while leaving
`Cloud/.cinna/account.json` trackable in exactly the workshops the legacy support exists to
serve** — the one population the back-compatibility promise was made to.

Both patterns ship in the file. Grep output, not a restatement:

```
$ grep -n "cinna/" docs/local_agent_kit/templates/root/gitignore
17:# Cloud account workspaces: `.cinna/account.json` holds an account token, which is
19:# segment cannot match an empty one, so `Cloud/*/.cinna/` misses `Cloud/.cinna/`
23:Cloud/.cinna/
24:Cloud/*/.cinna/
```

The comment above them states the mechanism, names `git check-ignore` as the verification, and
forbids the collapse by name. **Phase 10's line has been corrected in place by this recorder**
so the instruction and the artefact no longer disagree — a plan that still says one pattern is a
loaded gun for any later re-scaffold run.

**26b — the shape, and it outranks the fix: this was not missing knowledge, it was knowledge
present in one artefact and absent from another.** `layout.json`'s
`cloud_import_excludes_notes` already spells out the same never-matches-empty reasoning, for the
`**/` form:

> Directory patterns are listed in BOTH the root-anchored and the `**/` form on purpose: a
> `**/`-prefixed directory pattern cannot match at the root, because the directory branch only
> tries path prefixes at least as long as the pattern, so `**/.mypy_cache/` alone would miss the
> root-level `.mypy_cache/` that is the only one that ever exists.

That is the identical fact, written down, in a file this feature ships — and it is why the gap
survived into a plan written by people **who had already met it**. A team does not stop making a
mistake by learning it once in one file; the knowledge has to be attached to the *shape* (a
wildcard segment in an ignore pattern) rather than to the file where it was first met. §0 Bucket
17's `**/`-prefixed-directory footgun answer-back section is the same fact for a third time.

**And it compounds with a deliberate decision, which is the generalisable half.** Ruling the
legacy flat layout **Changed** rather than **Breaking** is what gave the gap something to bite:
the ruling quietly **enlarged the set of paths the ignore rules had to cover** — from
`Cloud/<host>/.cinna/` to that *plus* `Cloud/.cinna/` — and nothing connected the two decisions.
The back-compatibility guarantee and the ignore list were settled by different steps of different
phases, and neither one's author was in a position to see the other's consequence. **State the
link, not the pattern: a compatibility promise is a promise about a path set, and every rule that
enumerates paths inherits the enlargement.** That is checkable at the moment the promise is made;
"remember the wildcard rule" is not.

**26c — D6 removed the key cinna-cli reads, and the failure mode is a silent fallback that reads
as a configuration choice. Third instance of the causation shape, and the sharpest.** All four
links verified in both repos (`/Users/evgenyl/dev/ml-llm/cinna-cli` read-only throughout):

1. `load_exclude_patterns:257` (`src/cinna/local_import.py`) walks up for `.cinna-kit/kit.json`
   (`KIT_INDEX = "kit.json"`, `:56`) and reads `(index.get("cloud_import") or {}).get("exclude")`.
2. **D6 removed that key from `kit.json` entirely.** `git diff docs/local_agent_kit/kit.json`
   shows the whole `"cloud_import": { "exclude": [...] }` block deleted, and the surviving
   top-level keys carry no `cloud_import`.
3. The read is guarded by `isinstance(from_kit, list)`, so a missing key is **not** an error and
   not a warning — `patterns` keeps its initial value and `origin` keeps its initial string. The
   run then prints `_line(f"  Exclusions:  {patterns_origin}"):448`, which now reads
   `Exclusions:  built-in default list` where it used to print the `kit.json` path. **That line is
   the whole problem: it announces a mode, not a degradation.** Nobody gets an error; the first
   symptom is a `.env.prod` sitting in a cloud workspace.
4. `kit.py export` takes the other route entirely — `contract_exclude_patterns()` (`kit.py:781`,
   reading `layout.json`'s `cloud_import_excludes`) plus `secret_file_rules()`, both resolved
   once in `cmd_export` before a byte is copied.

**One correction to the handed-over account, and it sharpens the finding rather than softening
it.** The removed `cloud_import.exclude` and cinna-cli's `DEFAULT_EXCLUDE:65` are
**content-identical — the same 11 entries in the same order** (`credentials/`, `.venv/`,
`.claude/`, `AGENTS.md`, `CLAUDE.md`, `app-data/`, `temp/`, `__pycache__/`, `*.pyc`, `.git/`,
`.DS_Store`). So **cinna-cli's behaviour did not change at the moment of removal, and it lost
nothing it previously had.** What D6 removed is the **hook** — the one mechanism by which
anything we publish could ever reach cinna-cli's exclude list without shipping a new cinna-cli.
Before D6 a corrected list in `kit.json` would have propagated on the next `kit.py refresh`;
after it, nothing we write reaches that tool. **The degradation is measured against the authority
that moved, not against the list that was deleted**, and the counts are the measurement,
re-derived here: `layout.json.cloud_import_excludes` **41** entries, `kit.py`'s
content-identical offline `DEFAULT_EXCLUDES:319` **41**, cinna-cli's `DEFAULT_EXCLUDE` **11**.
Calling it "it lost entries" would be both wrong and easier to dismiss.

**The gap that actually leaks, verified by reading both gates.** `assert_no_secrets:321` refuses
a path whose first segment is `credentials` or `app-data`, and one where
`rel.endswith(".env") or Path(rel).name == ".env"`. A root-level `.env.prod`:

- clears every one of the 11 patterns — the six directory patterns take `_matches`'s directory
  branch, which tests `parts[:-1]` and is empty for a root file; `AGENTS.md`, `CLAUDE.md`,
  `*.pyc` and `.DS_Store` miss on both basename and full path;
- clears both `assert_no_secrets` clauses: it is not under `credentials/` or `app-data/`, it does
  not *end* with `.env`, and its basename is not exactly `.env`.

**So suffixed dotenvs — `.env.local`, `.env.prod`, `staging.env` — pass both gates and travel,
whenever they sit outside `credentials/`.** `layout.json` predicts this in as many words, and the
prediction is the reason this is filed rather than argued:

> A glob list cannot express this rule, which is why it is here and not in
> `cloud_import_excludes`: enumerating suffixes leaks the first one nobody thought of
> (`.env.prod`, `.env.staging`), and no glob can say 'any `.env.<suffix>` except `.example`'.

`secret_files`' `dotenv` rule matches `basename_equals: [".env"]`, `basename_prefix: [".env."]`
and `basename_suffix: [".env"]`, `unless` the basename ends `.example` / `.sample` / `.template`
— the shape cinna-cli's two-clause check is missing.

[**Corrected after the fact, verified by execution against the real function, and the correction
narrows the finding rather than withdrawing it.** The sentence above lists `staging.env` among the
shapes that "pass both gates and travel". **It does not travel — it is already refused**, because
`"staging.env".endswith(".env")` is `True`, and that is exactly the clause 26c credits cinna-cli
with having. `<name>.env` is therefore *not* part of the gap; cinna-cli's `endswith(".env")` is a
working equivalent of the declared rule's `basename_suffix: [".env"]`.

**The gap is precisely one clause: `basename_prefix: [".env."]`.** Measured by importing the real
function from `/Users/evgenyl/dev/ml-llm/cinna-cli` (read-only, via that repo's own `.venv`, since
a bare `python3` lacks `click`):

```
.env.prod            TRAVELS      <- the leak
.env.local           TRAVELS      <- the leak
a/b/.env.prod        TRAVELS      <- the leak, at depth
staging.env          refused      <- the claim above is wrong here
.env                 refused
credentials/.env     refused
.env.example         TRAVELS      <- correct; the `unless` clause exists to keep it so
```

**Why a one-word error is recorded at this length.** The wrong example was the *most* persuasive
one in the bucket — a bare `<name>.env` reads as precisely the shape a two-clause check would
obviously miss — and it survived into a handover written for another repository before execution
caught it. **Reading the two clauses is what produced the error; running them is what found it**,
and from the inside those two activities feel identical. Note also that `.env.example` travelling
is correct by design, not a second leak: it is what the rule's `unless` block exists to preserve,
and it is the next thing a reader "fixes".

The finding itself stands and its priority is unchanged: `.env.prod` and `.env.local` travel, at
any depth, outside `credentials/`. Only the enumeration of shapes was wrong.]

**Filed in the cinna-cli follow-up register (26h) as a priority item, and NOT in the desktop
answer-back.** cinna-cli is a separate repo and out of scope per requirements §7; **recording it
is the remedy available to this run**, and the register exists so that recording is not the same
as losing it.

**Keep it distinct from the `{{KIT_VERSION}}` instance (Bucket 25c), because the two teach
different lessons.** There, **we published a wrong list and another team implemented it
faithfully** — their code is the correct implementation of our error, and the remedy is an
apology plus a caveat. Here, **we removed something a consumer depended on**, and the failure
mode is not a wrong value but a *silent fallback that looks like a configuration choice*. The
first is caught by reviewing what we publish; the second is caught only by asking **who reads
this key** before deleting it — and nothing in this repo could have answered that, because the
reader lives in another one.

**26d — benign, and recorded AS benign so it does not start an investigation later.**
cinna-cli's `load_manifest` refuses a manifest whose `schema_version` exceeds its
`SUPPORTED_SCHEMA_VERSION = 1` (`:137-145`), and our template manifest **no longer emits the
field** (`git diff docs/local_agent_kit/templates/agent/cinna-agent.json` shows
`-  "schema_version": 1,`). **The read is `data.get("schema_version", 1)`**, so an absent field
resolves to `1` and the gate `schema_version > SUPPORTED_SCHEMA_VERSION` is false. A
new-contract folder passes. *"They gate on a field we deleted"* is exactly the sentence that
causes an unnecessary scare, and it is why the negative result is written down at the same
length as a positive one would be.

**26e — `guides/11-go-cloud.md` carried four stale claims, all now fixed, and one thing was
deliberately kept.** Verified against the diff and against the code each claim describes:

1. It said `kit.py export` *"clears the `cloud` block for you"*. `cmd_export` carries a comment
   forbidding exactly that, quoted rather than paraphrased:

   ```
   $ grep -n "must not be made to\|Do not reintroduce a rewrite" docs/local_agent_kit/tools/kit.py
   3727:    # Export does NOT write the manifest, and must not be made to. It used to
   3732:    # Do not reintroduce a rewrite to strip a deprecated `cloud` key from the
   ```

   The guide now says the manifest is copied **unchanged**, which is also what the command
   prints on success.
2. It pointed at `kit.json`'s `cloud_import.exclude` — the key 26c shows was removed — in both
   the step-8 prose and the manual-fallback step 4.
3. It instructed hand-writing the deprecated `cloud` block as the sole record of a publication.
   **The precise state now, because "fixed" would overstate it:** the guide still instructs
   writing that block, as the *second* of two records, with the deprecation stated and the reason
   given. The defect was that it was the only one; `publications.json` is now the first.
4. It claimed one exclude list applied *"in both routes"*. That section is now titled **"The two
   routes do not exclude the same paths"** and states the divergence, including the suffixed-
   dotenv gap of 26c.

**What was deliberately kept, and it must not be "tidied" later.** Step 8's sentence that
`cinna agent import` stamps a `cloud` block stays, because cinna-cli both **writes** it
(`stamp_cloud_block:394`, setting `platform_url` / `agent_id` / `imported_at`) and **reads** it
(`run_agent_import`: `cloud_block = manifest.get("cloud") or {}`, `known_agent_id =
cloud_block.get("agent_id")`, `:437-438`). **Clearing it makes the next `--update` create a
second agent** rather than update the existing one. A deprecation that a live consumer still
resolves by is not a deletion candidate, and the guide now says so in place.

**26f — two claims carried in this plan that did not hold against the tree.**

*(i) There is no "extended cache tuple".* Phase 3 step 2 says *"Extend the cache tuple to carry
the contract tarball too"* and Phase 13 step 3 tells the tech-doc writer to document *"the
extended cache tuple"*. What landed is a five-field `NamedTuple`, `_KitBuild:166`
(`version`, `rendered`, `tarball`, `contract_tarball`, `contract_defect`), and **its docstring
gives the reason the tuple was rejected**:

```
$ grep -n "A record rather than a bare tuple" -A 2 backend/app/services/cli/local_agent_kit_service.py
169:    A record rather than a bare tuple because it grew a fifth field: positional
170-    unpacking with a row of underscores is how a later edit silently reads the
171-    contract tarball as the kit one.
```

§0 Bucket 12 already records the outcome correctly; the stale wording survives only in the two
phase steps, and **Phase 13 has not run**, so it would have carried the rejected shape straight
into the tech doc as the documented one — teaching a reader the structure the code deliberately
refused. **Both phase lines corrected in place by this recorder.**

*(ii) An agent retracted its own claim that the desktop's fallback already reads
`CONTRACT_VERSION`, and the retraction is correct.* `readVersionAt:131`
(`src/main/kit/contractStore.ts`, read-only) reads `kit.json`'s `contract_version` first and then
falls through to `VERSION_FILE`, which is `const VERSION_FILE = 'VERSION'` (`:38`). Requirements
D2 states the change as **owed**: *"The desktop must change its fallback from `VERSION` to
`CONTRACT_VERSION`."* It stays on the required-changes list.

**Record the class, because it is the one that costs another team real work: documenting an owed
change as though it had landed can make them believe they have already done it.** It is the
stale-status mechanism of the Standing checklist's position half, pointed at *someone else's*
backlog — and it is worse there, because we are the only party who can see that the item is still
open and they are the only party who can close it. **The retraction is the recordable event, not
the error**: an agent re-deriving its own claim caught it, which is the same self-caught shape as
Buckets 21f and 25b, and it is the mechanism this run keeps finding to be the one that works.

**26g — an instrument worth generalising, SHARPENED into the existing non-vacuity rule rather
than added as a second one.** An agent proved a TypeScript typecheck non-vacuous by recognising
that **a clean grep is exactly what a file `tsc` never compiled also produces** — so it appended
a deliberate type error, confirmed the error appeared, restored the file, and diffed to prove the
restore. The general form, which is what the checklist now carries: **an absence of complaints is
evidence only once you have shown the tool would have complained.** Same family as the
reverted-copy differential (Bucket 11) and the md5-compared container copy (Bucket 21), both of
which the checklist bullet already named — which is precisely why this belonged **in** that
bullet: three instances of one rule stated once is a rule; one rule stated three times is how a
document stops being read (Standing checklist, position half).

**Recorded with its own limit, because the rule in 26a's neighbourhood applies to this bucket
too.** The proof was a *process*, and its artefact was deliberately restored, so **the only
verifiable residue is the method's shape** — this entry records a technique, not a checked
artefact, and it is marked as such rather than dressed as verified evidence.

**26h — the cinna-cli follow-up register, opened here.** cinna-cli is a separate repo, out of
scope per requirements §7. These are **not** answer-back items — sending them to the desktop team
would misfile them against a team that cannot act on them — and they are **not** work this run
may do. Recording them is the whole remedy, so the register is the deliverable:

- **PRIORITY. `cinna agent import` no longer receives our exclude list, and says so in language
  that reads as a mode.** Full mechanism in 26c. Two separable fixes: teach it `layout.json`'s
  `cloud_import_excludes` and `secret_files` (the authority), and — independently, because it is
  the half that leaks today — extend `assert_no_secrets` from its two dotenv clauses to the
  `dotenv` rule's three, so `.env.prod` outside `credentials/` cannot travel. Until then the
  `Exclusions:  built-in default list` line should read as the degradation it is.
- **`cinna login` in an empty folder makes that folder the workspace.** `_login_new_account:652`
  takes `--dir` when given, else `cwd` when `_dir_is_empty(cwd)`, else prompts for a subfolder.
  A user who follows *"make the workshop, then log in"* literally and runs a bare `login` from an
  empty root gets the flat layout.
- **`cinna login` inside an existing workspace ignores `--dir`.** `run_login:713` calls
  `find_account_root()` (`account.py:90`, walking up from cwd for `.cinna/account.json`) and, on a
  hit, refreshes in place. **Correction to the handed-over claim: it is not silent.** It emits
  `console.warn("Already inside an account workspace — refreshing it in place (domain / --dir
  ignored).")`. The claim was that both behaviours together would have made the plan's §5
  instructions produce the flat layout; **the mechanism holds, the "silently" does not**, and
  `guides/11-go-cloud.md` step 5 already describes it accurately ("ignores `--dir` with a
  warning"). Recorded because a register entry overstating a tool's quietness is the kind of
  detail that gets the whole entry discounted.
- **OPEN QUESTION, not a defect: `layout.json`'s `scaffold_ignore_files.root` has no reader in
  `kit.py`.** `scaffold_ignore_files():1083` hard-reads `block.get("agent")` and nothing else; the
  root pair (`gitignore` → `.gitignore`) is honoured only by prose, at `START.md:52`. That may be
  correct — `kit.py new` creates agents, and the root scaffold is a one-time human step — but it
  means a declared contract entry has no programmatic consumer, which is the state a second host
  reading `layout.json` would not expect. **Confirm intended rather than a missed consumer**; do
  not "fix" it by wiring a reader before that answer exists.

**26i — a tree-state fact, recorded plainly because the next reader will meet it cold.** Six
previously-untracked files are now **staged** — `git status --short` shows `A ` on
`docs/local_agent_kit/CONTRACT_VERSION`, `docs/local_agent_kit/layout.json`,
`docs/local_agent_kit/assistants/cinna-desktop.md`,
`docs/local_agent_kit/schema/publications.schema.json`, and both plan documents
(`docs/plans/local_agent_kit_desktop_contract_plan.md`,
`docs/plans/local_agent_kit_desktop_contract_requirements.md`). Verified alongside it: **nothing
is committed** (`HEAD` is `34852c2c`, unmoved), and **the stash is empty** (`git stash list`
returns nothing).

**Re-derived at the end of this run rather than quoted forward from its start — and it had
already moved, which is the point of re-deriving it.** This document now reports `AM` rather than
`A `: still staged, with **this bucket's own writes** sitting unstaged on top of the staged
addition. Nothing was unstaged and nothing was lost; the marker changed because the recorder
edited the file it was describing. **A tree-state claim goes stale faster than any other kind,
including by the act of writing it down**, so state which marker you saw and when, never "the
file is staged" as a standing property. The other five are unchanged at `A `. Independently, a
sibling agent's knowledge-mirror sync landed mid-run, adding modifications under
`backend/app/env-templates/` and to `.gitignore` and `sync_platform_knowledge.py`; **none of them
are staged**, and this recorder wrote no file but this one.

**No claim is made about who staged them, and the abstention is the same one Bucket 25f makes for
the same reason:** the tree is shared by several agents, staging leaves no authorship evidence,
and mtimes are not authorship evidence. The coordinator did not instruct it. It is recorded
because a reader who finds a staged set they did not create will otherwise spend the time working
out whether something was committed — and because **unstaging would require `git reset`, which
this run's Standing checklist prohibits outright.** Leaving them staged is the correct action, not
an omission.

### Bucket 27 — the sync proven by delta, a plan premise that was wrong about its own mechanism, and the run's rule biting the coordinator

**Status: VERIFIED (recording run, final round).** Every figure below is re-derived at the point
of use; every protection-shaped claim carries its grep output. Two handed-over figures did **not**
re-derive and are recorded as failures rather than corrected away (27h). Where a before-state no
longer exists on disk it is reconstructed from `HEAD` and said to be so.

**27a — Phase 11's sync, proven by delta rather than by a green tick.** The mirror's before-state
is recoverable because the whole feature is uncommitted: the mirror lives under
`backend/app/env-templates/`, which is tracked, so `HEAD`'s copy **is** the pre-sync mirror. All
four handed-over deltas were re-derived that way, not quoted:

```
$ M=backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit
$ for f in $(git ls-tree -r --name-only HEAD -- $M/templates/agent); do git show HEAD:$f; done \
    | grep -o "{{name}}\|{{slug}}" | wc -l          # BEFORE
      14
$ grep -rn "{{name}}\|{{slug}}" docs/local_agent_kit/templates/agent/ | wc -l          # AFTER
       0
$ ...same form, "{{[A-Z_]*}}"                        # BEFORE 2, AFTER 19
$ ...same form over the whole mirror, "MyAgents"      # BEFORE 8, AFTER 0
```

Set-and-byte equality, which is the claim that actually matters, holds:

```
$ find docs/local_agent_kit -type f -not -path "*__pycache__*" | wc -l
      56
$ find $M -type f -not -path "*__pycache__*" | wc -l
      56
$ diff <(cd docs/local_agent_kit && find . -type f | sort) <(cd $M && find . -type f | sort)
SETS EQUAL
$ diff -r -x "__pycache__" docs/local_agent_kit $M
BYTES EQUAL (ex-pycache)
```

**One count did not re-derive, and the discrepancy is informative rather than a defect.** The
handover reported the file count as **53 → 56**; `HEAD`'s mirror carries **52 tracked** files.
The difference of one is consistent with a disk count that included the untracked
`tools/__pycache__/kit.cpython-313.pyc` — which is 27b's subject and is gitignored, so
`git ls-tree` cannot see it. **Consistent, not proven**: the pre-sync disk state is gone and no
evidence for it survives. Recorded at the accuracy available.

**27b — the second mirror tree was stale, and a `local-kit/`-only check would have passed over
it.** This is the more valuable half of Phase 11 and it is not a count. `make
sync-platform-knowledge` runs **three** steps in one invocation, and
`docs/application/local_agent_kit/local_agent_kit.md` rides the **first** (`sync_docs()`) into
`…/knowledge/platform/application/local_agent_kit/` — a different tree from `local-kit/`
entirely. Verified byte-identical to source now:

```
$ diff docs/application/local_agent_kit/local_agent_kit.md \
       backend/.../knowledge/platform/application/local_agent_kit/local_agent_kit.md
IDENTICAL
```

And the null that stops a future reader filing a non-defect: **`local_agent_kit_tech.md` is never
mirrored, by design.** Grep output rather than a restatement:

```
$ grep -n "_tech" .cinna-core-kit/scripts/sync_platform_knowledge.py
10:  1. docs/application/ and docs/agents/ — business-logic docs (excluding *_tech* files)
79:    """Copy business-logic docs (excluding _tech files). Returns file count."""
86:            if "_tech" in md_file.name:
250:    print("[1/3] Copying feature documentation (excluding _tech files)...")
```

**The shape, stated so it outlives this instance: a verification that names the tree it checks is
checking a tree, not a sync.** One command wrote two trees; the check covered one; the check
would have reported success. Phase 11 item 1 already carries the warning (added after Bucket 23e),
and this round is the first execution in which it was load-bearing.

**27c — the plan's `.pyc` premise was wrong, and the correction outranks the fix.** Phase 11
item 8 asserted that a compiled `kit.cpython-313.pyc` "rides `make sync-platform-knowledge` into
the snapshot". **It does not, and never did.** `KIT_SKIP_DIRS` already contains `__pycache__`,
and the walk applies it **before** publishability is ever consulted:

```
$ grep -n 'KIT_SKIP_DIRS = \|KIT_SKIP_DIRS &\|"\.pyc",\|stray ' .cinna-core-kit/scripts/sync_platform_knowledge.py
137:KIT_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv", "node_modules"}
147:# stray `tools/kit.pyc` written beside its source. Compiled bytecode is never
157:    ".pyc",
202:        if KIT_SKIP_DIRS & set(rel.parts):
```

`_is_publishable` is defined at `:162` and called at `:215`; the `KIT_SKIP_DIRS` `continue` at
`:202` runs first. **So the general fix landed for a reason the plan did not state.** What adding
`.pyc` to `KIT_DENY_SUFFIXES` actually closes is a **bare `tools/kit.pyc` written beside its
source** — no directory rule catches that, because it is in no `__pycache__/`. The shipped comment
says exactly this, which is why it is quoted rather than paraphrased (`:144-148`): *"this list is
the suffix denylist and `_is_publishable` is ALLOW-BY-DEFAULT: `KIT_SKIP_DIRS` stops bytecode that
sits inside a `__pycache__/` directory, but nothing stops a stray `tools/kit.pyc` written beside
its source."*

**The provenance, verified end to end, because it explains why the artefact keeps coming back.**
The bytecode is regenerated by **the test suite itself**, which imports the kit by file location
(`spec_from_file_location("cinna_local_kit_tool", KIT_PY)`,
`backend/tests/unit/test_local_kit_tool.py:176`). Both copies exist right now, with different
bytes and different mtimes, and the chain that produces each is checkable:

```
$ docker compose exec -T backend id -u
0
$ docker compose exec -T backend python -c "import tests.unit.test_local_kit_tool as t; print(t.KIT_DIR)"
/app/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit
$ grep -n "backend/app:/app/app" docker-compose.override.yml
22:      - ./backend/app:/app/app
```

`docs/` is not mounted, so the container's kit resolves to the **snapshot**; `backend/app` **is**
mounted, so the container's bytecode write lands on the host tree. The host run compiles the
source tree's copy. That is the whole loop: **`docs/` being unmounted is simultaneously the reason
the mirror must be fresh and the reason the mirror accumulates bytecode.** Both files are ignored
(`.gitignore:43` for the source tree, `backend/.gitignore:1` for the mirror), so neither is one
`git add -A` from a commit any more.

**Phase 11 item 8 corrected in place by this recorder.** An uncorrected item 8 is worse than a
missing one for the reason Bucket 24b gives: it is a *false explanation of correct code*, and it
would have sent the next reader looking for a sync path that cannot exist.

**27d — an agent removed its own redundant fix, and that is the recordable event.** It first added
mirror-side ignore rules alongside the source-side ones, then found `backend/.gitignore` already
covered those paths, and **took them back out**. The residue is checkable in both directions —
the source-side rules are there with a comment stating why they are needed, and no mirror-side
rule exists at all:

```
$ grep -rn "docs/local_agent_kit/\*\*" .gitignore
43:docs/local_agent_kit/**/__pycache__/
44:docs/local_agent_kit/**/*.pyc
$ git diff backend/.gitignore
(no output — unmodified)
$ git check-ignore -v <both bytecode paths>
.gitignore:43:docs/local_agent_kit/**/__pycache__/	docs/local_agent_kit/tools/__pycache__/kit.cpython-313.pyc
backend/.gitignore:1:__pycache__	backend/.../local-kit/tools/__pycache__/kit.cpython-313.pyc
```

**Why this is the false-rationale class and not a tidiness anecdote.** A redundant ignore rule
costs nothing at runtime. What it would have shipped is **a comment asserting a protection that
was already someone else's** — a reader meeting the mirror-side rule would believe the mirror is
protected *by this feature's rules*, and would then be free to move or delete them. Same family as
`contract_version()`'s docstring justifying a correct fallback with a false reason (Bucket 24b):
the mechanism is right, the stated ownership is wrong, and **nothing can ever fail to reveal it**.
The removal is the right move precisely because the surviving comment now says the true thing —
that `backend/.gitignore` is backend-scoped, which is why the source-side rules are needed at all.
**Generalise it as a preventive rule, not a review finding: before adding a protection, check
whether one already holds — and if it does, adding a second does not double the protection, it
halves the next reader's ability to tell who owns it.**

**27e — Phase 12's final test position, re-derived rather than accepted.** All three container
figures and the host figure were re-run by this recorder, not carried forward:

```
$ docker compose exec -T backend python -m pytest tests/unit/test_local_kit_tool.py -q
158 passed, 10 skipped in 35.02s
$ docker compose exec -T backend python -m pytest tests/api/cli/test_local_agent_kit.py -q
105 passed in 29.55s
$ docker compose exec -T backend python -m pytest tests/api/ -k "local_agent_kit or agent_start" -q
106 passed, 1930 deselected in 30.85s
$ python -m pytest tests/unit/test_local_kit_tool.py -q          # host
168 passed in 23.30s
```

158 + 10 = 168: **the same tests, with ten of them skipped in the container and none on the
host.** The non-vacuity evidence for the container runs is the `KIT_DIR` line in 27c — the
container exercised the **snapshot**, which is exactly why 27a's byte equality is what makes these
numbers mean anything. The host run resolved `docs/local_agent_kit/` (rung two of `_find_kit_dir`)
and therefore exercised the working tree.

**The mechanism behind the ten skips is the part worth keeping.** The container runs as **root**
(`id -u` → `0`), where `chmod 000` does not stop a read, so every permission case would pass while
testing nothing. The helper does not ask `os.geteuid()`; it **provokes** the error and skips with
a reason that names the problem:

```
$ grep -n "def require_enforced_permissions" -A 1 backend/tests/unit/test_local_kit_tool.py
2443:def require_enforced_permissions(tmp_path: Path) -> None:
2444-    """Skip unless this process is actually stopped by mode bits."""
```

Its skip message is *"this process is not stopped by file permissions (running as root?), so every
permission case here would pass without testing anything"*. **Ten vacuous greens were designed out
rather than discovered** — and the distinction from every other non-vacuity instrument in this
document is worth stating: the others *provoke a complaint to validate a probe*; this one
**provokes a complaint to decide whether the test may run at all**, converting a silent pass into
a visible skip. A `geteuid()` check would have been the same idea one step further from the
evidence, and would have been wrong under any capability model that separates the two.

**27f — the blocking token-egress differentials are now committed to the tree.** Phase 8's two
most valuable findings were, until this round, protected by nothing but the code happening to be
correct. Both now exist as tests, and the names say what they are:

```
$ grep -n "def test_chat_.*guard_is_load_bearing" backend/tests/unit/test_local_kit_tool.py
1310:def test_chat_refuses_a_cross_origin_redirect_and_the_guard_is_load_bearing(
1352:def test_chat_ignores_an_environment_proxy_and_the_guard_is_load_bearing(
```

Each is **two runs against one stub topology**, with the guard removed in a scratch copy and then
present in the shipped kit. Guard removed: the foreign host / the recording proxy **receives the
bearer token** (`assert MARKER_TOKEN in authorization`), and the redirect case additionally
**exits 0 printing the foreign host's answer** — the comment on that assertion is the finding
stated as a property: *"Sharper than 'it leaks': the leaking run LOOKS like it worked."* Guard
present: non-zero exit and `assert foreign.requests == []` / `assert proxy.requests == []` — **the
recorder receives no request at all**, which is a stronger claim than "no token in the request".

**Each was proven load-bearing by a behavioural failure, not an anchor failure**, and the
scratch-copy helper enforces that distinction itself: it asserts the anchor appears exactly once,
that the patch changed something, that the anchor is **absent** from the readback and that the
replacement is **present** — four checks before the differential may report anything. Its docstring
gives the reason, which is this document's own Bucket 21f: *"a reverted-copy differential must
prove the revert happened before it may report what the revert caused."*

The leak assertion is **three classes of value across five assertions** over combined
`stdout + stderr`: the token, every other state-file value (`MARKER_EXTRA_VALUES`, plus
`MARKER_CHAT_PATH`), **and the `api_base_url` itself plus its bare `host:port`**. The third is the
one a reader drops, and the test says why: *"D3 says a tool never prints the state file's
contents, and the URL *is* contents. Implement 'never print the token' alone and the URL goes out
in the next error line with the rule believed kept."*

**27g — the sweep and its nulls, which are what make the count defensible.** Four axes were swept
across both test files — scaffold-token **case**, the token **set**, `schema_version` **existing**,
and `publications` **in the manifest**. Result: **one instance beyond the plan's three — this
module's own `PLATFORM_TOKEN_RE` — and no others.** The absences are asserted at their sources
rather than reported (`assert "schema_version" not in kit_json`, `not in template`,
`assert "publications" not in template`, `assert "publications" not in schema["properties"]`), and
all four hold on re-derivation. In the serving file `publications` **never appears at all**:

```
$ grep -c "publications" backend/app/services/cli/local_agent_kit_service.py
0
```

**An explicit null, recorded at the same length a positive would get**, for Bucket 26d's reason.
(The word survives once in `schema/cinna-agent.schema.json:219`, inside the `cloud` block's
deprecation text that *points at* `publications.json` — the schema's `properties` do not carry it,
which is what the assertion checks.)

**The guard is locked by an AST walk rather than a grep, and the reason is a caught-in-the-act
instance of a documented trap.** The first draft grepped for the retired name `PLATFORM_TOKEN_RE`
and **failed on its own explanation of why the name is retired** — the module comment that
forbids the name necessarily contains it. That is the Standing checklist's absence-probe
false-failure (i), met live. The fix was not a cleverer pattern: it was to ask a question prose
cannot answer, walking module-level `re.compile` assignments and asserting the only brace-bearing
one is `ANY_TOKEN_RE`. The test says so in place: *"Reading the hit is what settles it; an AST
walk cannot see prose at all."* **Where an absence probe keeps hitting its own documentation,
change the instrument's domain, not its pattern.**

**27h — two figures that did NOT re-derive. These are the run's own rule biting, and both are
recorded as instances rather than quietly corrected.**

*(i) The `harmonis` count moved 3 → 5.* The Standing checklist's protection bullet states, as of
its own writing, that the grep "now returns **three** lines". It returns five:

```
$ grep -n "harmonis" docs/local_agent_kit/tools/kit.py
710:    purpose. Do not harmonise them.** Here, a `kit.json` that cannot be parsed
763:    Do not harmonise them.** Every value this file supplies has a correct
805:    purpose. Do not harmonise them.** There, a contract rule this build cannot
950:    naming each other and saying not to harmonise them. Here:
1136:    directions, on purpose. Do not harmonise them.** Here, a rule this build
```

**And the reading matters more than the number: nothing regressed.** The original pair
(`contract_exclude_patterns:805`, `secret_file_rules:1136`) still carry their cross-reference
notes, and `desktop_state_file:950`'s observation about *those two specifically* is still
accurate. The two new lines are a **second** opposed pair (`kit_config:710`, `layout_config:763`)
that adopted the same convention. **So the figure went stale by the good thing happening** — a
convention spreading — which is the least suspicious way for a number to move and therefore the
one nobody re-checks. The checklist's own closing instruction (*"Re-run the grep before repeating
any of this"*) is what caught it; **that bullet has been sharpened in place** rather than
duplicated.

*(ii) The coordinator relabelled a figure that Phase 11 had measured and labelled correctly, and
the relabelling is the pointed part.* Both test agents were briefed with **"19 UPPERCASE scaffold
tokens"**. Nineteen is the count of uppercase tokens **under `templates/agent/`** — that
re-derives exactly (27a) — but *"scaffold"* is the coordinator's word, not Phase 11's. Broken out:

```
$ grep -rho "{{[A-Z_]*}}" docs/local_agent_kit/templates/agent/ | sort | uniq -c
   1 {{CONTRACT_VERSION}}
   1 {{CREATED_AT}}
   1 {{DESCRIPTION}}
   1 {{ID}}
   1 {{KIT_VERSION}}
  11 {{NAME}}
   3 {{SLUG}}
```

**One of the nineteen is `{{KIT_VERSION}}`, and Bucket 24a is what it is: the seventh token is
filled by the SERVER before anyone sees it, not by the scaffolder.** Two agents independently
re-derived the count and reported different, correctly-scoped figures rather than echoing the
brief.

**Record the coordinator's error plainly, and record why it is pointed rather than trivial.** The
whole round exists to remove a **shape-versus-provenance conflation** — the retired
`PLATFORM_TOKEN_RE` classified tokens by their CASE when case had stopped carrying provenance
(27g, and the source comment at `test_local_kit_tool.py:103-120`). **The same conflation reappeared
inside the figure used to describe the fix for it.** A count taken by shape ("uppercase tokens
under this directory") was passed on with a provenance label ("scaffold tokens"), by the party who
had just read the finding that the two are not the same question.

**And one more turn of the same screw, which this recorder owes on itself.** The brief that
commissioned this bucket phrased the correction as *"`{{KIT_VERSION}}` … is not a scaffold
token"*. **That is also imprecise, in the same direction.** `KIT_VERSION` **is** a member of
`SCAFFOLD_TOKENS` (`kit.py:152-160`), deliberately, and `kit.py`'s own comment there settles why
membership cannot answer the question: *"A list cannot classify a member it contains, and this one
contains `KIT_VERSION`. What settles the question is PROVENANCE."* The accurate statement is
**not** that it is outside the set, but that **it is the one member the scaffolder does not fill**.
Three parties in a row reached for a set or a shape where only provenance answers — which is the
strongest available evidence that the conflation is *attractive* rather than careless, exactly as
Bucket 24b reads the twice-made false protection claim.

[**SUPERSEDED 2026-09-03 — the ruling below has inverted, and the reason is worth more than the
fix.** This bucket ruled the defect a both-hosts-one-change fix, on the ground that a one-sided
fix converts a shared blind spot into a cross-host divergence. That reasoning was right, and it is
exactly what happened. **cinna-cli fixed its half unilaterally** (commit `c587fc7`), and it did so
**because the handover we wrote them told them to and did not say it needed coordinating** — the
instruction was ours, they followed it, and no blame attaches to them. Their resolution is also
better than either option this bucket offered: they strip *and* warn, where the bucket framed the
choice as strip **or** reject.

Measured before the fix:

```
cinna-core  matches_pattern('temp/ ', 'temp/x.txt')  ->  False   (matched nothing)
cinna-cli   matches_pattern('temp/ ', 'temp/x.txt')  ->  True    (strips, and warns)
```

**So "leave ours alone" stopped being the safe option and became the divergence itself.**
cinna-core has now adopted the same behaviour: `matches_pattern` strips before the branch test so
the branch and the body derive from the same text, and `contract_exclude_patterns()` strips at load
and prints a warning naming the pattern. Verified by differential — the unit tests failed against
the pre-fix mirror and pass against the fixed one, with host/mirror md5 parity confirmed, since
`docs/` is not mounted into the backend container.

**The transferable half is not about whitespace.** A finding recorded as "deliberately not fixed,
pending coordination" has a **dependency on the other party not acting** that nothing in the record
enforces — and here the same author both wrote the coordination requirement and, in a different
document for a different repo, instructed the unilateral fix that broke it. **A cross-repo hold is
only as good as the instruction sent to the other repo**, so when a finding is deferred pending
coordination, the deferral belongs in every handover that could reach a party able to act on it,
not only in the plan that records it.]

**27i — a latent defect found and deliberately NOT fixed.** `matches_pattern` selects its
directory branch from the **raw** pattern with `pattern.rstrip().endswith("/")` — whitespace
tolerant — while `normalize_rel_path` strips only `/` (`.lstrip("/").rstrip("/")`). So trailing
whitespace survives into the pattern **body** as a segment of its own, and the pattern then matches
nothing. Reproduced against the shipped `kit.py`, not argued:

```
$ normalize_rel_path("app-data/ ")  ->  'app-data/ '   # segments: ['app-data', ' ']
$ matches_pattern("app-data/",  "app-data/x.json")  ->  True
$ matches_pattern("app-data/ ", "app-data/x.json")  ->  False
$ matches_pattern("app-data/ ", "app-data")         ->  False
$ matches_pattern("app-data/ ", "app-data/sub/y")   ->  False
```

**Latent, and the mitigation is real:** no shipped `cloud_import_excludes` entry has stray
whitespace, and a test now asserts the shipped list stays clean
(`test_trailing_whitespace_selects_the_directory_branch_but_not_the_body:1612`, whose body reads
`untrimmed = [p for p in layout["cloud_import_excludes"] if p != p.strip()]` and asserts it empty).
A hand-edited contract, though, would silently drop a directory from the exclude set and move the
`content_hash` — with every individual step still looking like it worked.

**One correction to the handed-over framing, and it changes who is at risk.** The desktop is
**identically affected**, not divergently: `matchesPattern` uses `pattern.trimEnd().endsWith('/')`
(`src/main/kit/layout.ts:275`) and `normalizeRelPath` strips only slashes (`:121-127`, four
`replace` calls, all on `/` or `./`) — read-only, in their tree. So the two hosts would **shrink
the exclude set together** and agree on the wrong hash, rather than disagreeing. That is worse in
one specific way and better in another: no cross-host mismatch alarm would ever fire, and the
files that escaped would escape on both sides. **A faithful port inherits the original's defects
faithfully** — which is the property the port was written for, so it is not a criticism of the
port.

**Not fixed, and the reason is a rule rather than a scope quibble:** `kit.py` was not the test
agent's file to edit. It is filed here so the next `kit.py` owner meets it; the one-line fix is to
normalise the pattern before choosing the branch, on **both** hosts in the same change, since a
one-sided fix is precisely the divergence the symmetry currently prevents.

**27j — final tree state, re-derived at the end of this run.** `HEAD` is unmoved at
`34852c2c9bbc539a5ff22860b9c35248b2db7708`, `git stash list` returns nothing, and **nothing is
committed**. `git status --short` counts **79** paths, against Bucket 26i's tree; the jump is the
regenerated mirror, whose files are tracked and therefore show as ` M`. Broken out by index/worktree
marker so the claim is checkable rather than a total:

```
$ git status --short | awk '{print substr($0,1,2)}' | sort | uniq -c
  69  M
   4 ??
   5 A
   1 AM
```

**Six files are staged** — the four new kit artefacts (`CONTRACT_VERSION`, `layout.json`,
`assistants/cinna-desktop.md`, `schema/publications.schema.json`), the requirements document, and
this plan document, which reports `AM` because this recorder is editing the file it is describing.
Identical to Bucket 26i's set, and its `AM` observation reproduced for the same reason it gave.
**The coordinator did not instruct the staging and this recorder makes NO authorship claim about
it**, for Bucket 25f's and 26i's reason: several agents share this tree, staging leaves no
authorship evidence, and **mtimes are not authorship evidence**. Leaving them staged is the
correct action — unstaging would require `git reset`, which the Standing checklist prohibits
outright.

**One tree-state observation that is evidence rather than bookkeeping.** The two bytecode files of
27c carry mtimes of `10:04` (mirror) and `10:15` (source tree), and **this recorder's four test
runs did not change either** — CPython found both caches valid and rewrote neither. So they are
residue of earlier runs, and their divergent bytes (192441 vs 192429) are the two platforms'
compilers, not a stale copy. **A tree-state claim goes stale faster than any other kind**
(Bucket 26i); this one was re-derived after the last test run rather than before the first.

## §1 Corrections to the requirements brief

Recorded here so no phase silently works around them.

1. **Every `kit.py` line number the brief and the handover cite is exact.** Verified against
   the current tree: `kit_config:227`, `cloud_import_excludes:234`, `validate_manifest:355`,
   `substitute_tokens:671`, `restore_scaffold_ignore_files:685`, `cmd_new:708`,
   `_validate_requirements:831`, `template_description:918`, `_validate_cloud_readiness:934`,
   `validate_agent:986`, `cmd_validate:1017`, `_rungs_present:1062`, `_print_table:1107`,
   `cmd_list:1119`, `_parse_remote_version:1196`, `_locate_extracted_kit:1212`,
   `_swap_kit_tree:1222`, `cmd_refresh:1240`, `is_excluded:1304`, `cmd_export:1335`,
   `new_parser:1417`. Also `account_create_agent` at `routes/cli.py:859`. **No drift.**
2. **D8's rider is wrong.** See A1 — five more references outside the kit, one of them a
   user-facing frontend surface. Phase 10 covers them.
3. **D6 cannot ship as written.** See Bucket 2. Take the fallback branch.
4. **D6 also underestimates the code change.** Moving the list to `layout.json` is not
   enough: `ALWAYS_EXCLUDE` re-adds `credentials/` unconditionally at export
   (`cmd_export:1370`), and `is_env_filename` (`kit.py:127`) drops **`.env.example`**
   independently of any list — `".env.example".startswith(".env.")` is `True`. So the
   brief's "`credentials/.env.example` now travels" outcome is unreachable without editing
   both. Phase 4 handles this.
5. **The desktop's authored exclude list has a secret hole.** It carries `**/.env` and
   `**/.env.local` but **not** `**/*.env`, so `prod.env` / `staging.env` at any depth
   **travel** under their semantics. Our `is_env_filename` catches those today. Phase 4
   adds `**/*.env` to the shared list (it does not match `.env.example`, whose final
   segment ends in `.example`), and the answer-back reports it.
6. **§6's `Cloud/<host>/` is a code change too** — `cmd_list:1119`. See A6 and Phase 9.
7. **`_locate_extracted_kit:1212` requires `kit.json` AND `VERSION`.** The contract tarball
   ships no `VERSION` (D2), so that helper can never locate a contract tree. Harmless today
   (`refresh` only downloads `kit.tar.gz`), but do not reuse it for a contract install
   without changing the predicate. Noted, not changed.
8. **Correction 5 above is folded into D15.** The `**/*.env` gap it identifies is exactly
   what D15's declared dotenv rule in `layout.json` closes; see §0 Bucket 7b. Not tracked
   as a separate outstanding item from here on.

---

## Phase 1 — Contract data files

**Goal:** the contract exists as declared data. No behaviour changes yet, so this phase is
reviewable on its own.

**Files**

- **NEW** `docs/local_agent_kit/layout.json`
- **NEW** `docs/local_agent_kit/CONTRACT_VERSION`
- **EDIT** `docs/local_agent_kit/kit.json`

**Work**

1. `CONTRACT_VERSION` — a literal file containing `1.0.0` plus one trailing newline
   (6 bytes). **Hand-maintained. It is NOT a `{{TOKEN}}`**, unlike `VERSION`. Nothing in
   `LocalAgentKitService.placeholders()` may learn about it, and
   `LocalAgentKitService._content_version` must keep excluding only `{{KIT_VERSION}}`.
2. `layout.json` — start from the desktop's authored file
   (`/Users/evgenyl/dev/ml-llm/cinna-desktop/resources/cinna-kit-contract/layout.json`),
   2-space indent, trailing newline. Keep every block as authored:
   `contract_version`, `description`, `workshop` (`kit_dir`/`agents_dir`/`cloud_dir`/
   `root_files`/`roles[]`), `agent` (`manifest`/`prompt_files`/`command_catalog`/
   `status_file`/`roles[]`), `scaffold_ignore_files` + `scaffold_ignore_files_notes`,
   `desktop_owned`, `cloud_import_excludes` + `cloud_import_excludes_notes`,
   `local_command_runner`.
   **Four deliberate deviations from their file, each with a comment-free but documented
   rationale (the rationale lives in `CHANGELOG.md`, Phase 10):**
   - **a.** `cloud_import_excludes` **keeps `credentials/`** (Bucket 2). Remove their
     `credentials/.env` entry as redundant once the directory is excluded, or keep it as
     belt-and-braces — pick one and be consistent with the fallback list in `kit.py`.
   - **b.** `cloud_import_excludes` **gains `**/*.env`** (§1 correction 5).
   - **c.** `cloud_import_excludes_notes` — rewrite the sentence claiming
     `credentials/README.md` and `credentials/.env.example` travel. State the opposite and
     say why (the platform generates its own `credentials/README.md` and feeds it to the
     agent's prompt).
   - **d.** `desktop_owned` — the desktop authored this as a bare string array
     (`["app-data/desktop.json"]`). **D3 requires the object form** with `path`, `owner`,
     `contract_keys` (`api_base_url`, `agent_token`, `chat_path`) and `notes`, verbatim as
     the brief's D3 block gives it. Their `layout.ts` `parseLayout` normalises
     `desktop_owned` entries and is tolerant, but **confirm** their parser accepts the
     object form before shipping; if it does not, that is a required desktop-side change
     and goes in the answer-back rather than being downgraded here.
3. `kit.json` — add `"contract_version": "1.0.0"` (a literal, **not** a token) and
   **remove** the whole `cloud_import` block (D1/D6: one list, one place). Leave
   `kit_version`, `schema_version`, the URL tokens, `entry`/`index`/`manifest_schema`/
   `agent_template`/`root_template`/`tool`, `ladder`, `tool_command`, `runtime` untouched.
   Do **not** import the desktop's `refresh` block: our `kit_base_url` token already
   carries the instance origin, and duplicating it would create a second truth.

**Compatibility note that makes this work:** the desktop's `contractStore.ts` locates the
schema, layout and templates by **hard-coded constants**
(`schema/cinna-agent.schema.json`, `layout.json`, `templates/`) — not from `kit.json`. Our
existing paths already match all three. It reads only `kit.json.contract_version`
(falling back to `VERSION`), so our `kit.json` needs no other new key.

**Also confirms D2:** `isContractTree(root)` is `kit.json && layout.json` both present at
the root. A full kit install already puts `kit.json` at `.cinna-kit/` root; `layout.json`
joins it there. `<workshop>/.cinna-kit/` becomes a valid contract tree with no desktop
change. And `readVersionAt` reads `kit.json` first, so the workshop's `VERSION`
(kit version) is never mistaken for the contract version.

**Version-authority invariant to assert in a test (Phase 12):** `1.0.0` appears in exactly
three places and they must agree — `CONTRACT_VERSION`, `kit.json.contract_version`,
`layout.json.contract_version`.

**Reviewable when:** the three files are valid JSON/text, `1.0.0` agrees across all three,
`kit.json` has no `cloud_import` key, and `git check-ignore` reports the two new files as
not ignored.

---

## Phase 2 — Manifest schema

**Goal:** adopt the desktop's authored schema, prove only the expected deltas moved.

**Files**

- **EDIT** `docs/local_agent_kit/schema/cinna-agent.schema.json`

**Work**

1. Take
   `/Users/evgenyl/dev/ml-llm/cinna-desktop/resources/cinna-kit-contract/schema/cinna-agent.schema.json`
   as the starting point. **Keep our `$id`** (`https://cinna.dev/schema/cinna-agent.schema.json`
   — which happens to be identical to theirs; confirm and keep).
2. Diff the result against the current file and confirm **exactly** this delta set and
   nothing else:

| # | Change | Detail |
|---|---|---|
| 1 | `required` narrows | `["schema_version","name","slug","description"]` → `["name","slug","description"]` |
| 2 | top-level `allOf` added | `if` (has `schema_version` AND no `contract_version` AND no `id`) `then` {} `else` `required: ["contract_version","id"]`. Its `$comment` names the three places the rule is encoded — keep the comment; it is the anti-drift device. |
| 3 | `schema_version` | `const: 1` **removed**, `deprecated: true` added, description rewritten to "tolerated legacy, nothing branches on it" |
| 4 | `contract_version` NEW | string, `^(0\|[1-9]\d*)\.(0\|[1-9]\d*)\.(0\|[1-9]\d*)(?:-[0-9A-Za-z-.]+)?$` |
| 5 | `id` NEW | string, UUID pattern `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$` |
| 6 | `created_at` NEW | `["string","null"]`, informational |
| 7 | `runtime` NEW | `["object","null"]`, `additionalProperties: true`, `model` / `credential` both `["string","null"]`, `permissions` object with unknown keys preserved. `credential` description states **reference, never a value** |
| 8 | `publications[]` NEW | array; entry requires `platform_url` + `agent_id` (both `minLength: 1`); optional+nullable `workspace`, `imported_at`, `updated_at`, `contract_version`, `content_hash` |
| 9 | `cloud` | retained, marked deprecated, still parsed |
| 10 | `kit_version` description | rewritten to say explicitly **never a gate** |

3. Everything else — `name`, `slug`, `description`, `example_prompts`,
   `router_trigger_prompt`, `prompts` (`additionalProperties: false`),
   `status_refresh_command`, `credentials[]` (the 12-value type enum, `env_prefix`
   pattern, `fields`, `optional`), `schedules[]` (cron pattern, the two conditional
   `allOf` branches), `handovers[]`, `features` — must be **byte-identical in meaning** to
   today. Any other difference the diff surfaces is a bug in the adoption, not a feature.

**Reviewable when:** the diff shows only rows 1–10; the file parses; the existing
`test_template_manifest_matches_the_shipped_schema` still passes (it will need the Phase 6
template update to keep passing — note the coupling, do not paper over it).

---

## Phase 3 — Backend: contract tarball and contract version endpoints (§8.1, D9)

**Goal:** serve the contract independently, inheriting the existing plumbing without
forking it.

**Files**

- **EDIT** `backend/app/services/cli/local_agent_kit_service.py`
- **EDIT** `backend/app/api/routes/local_agent_kit.py`

**Work**

1. In the service, add next to the existing `TARBALL_ROOT` / `TARBALL_FILENAME`:
   - `CONTRACT_VERSION_MEMBER = "CONTRACT_VERSION"`
   - `LAYOUT_MEMBER = "layout.json"`
   - `CONTRACT_TARBALL_ROOT = "cinna-contract"`
   - `CONTRACT_TARBALL_FILENAME = "cinna-contract.tar.gz"`
   - `CONTRACT_MEMBERS` — the D1 member list, expressed as exact names plus directory
     prefixes: `{"kit.json", "layout.json", "CONTRACT_VERSION", "CHANGELOG.md"}` and the
     prefixes `"schema/"`, `"templates/"`. **No `VERSION`** (D2).
2. `_build_or_cached` currently memoizes `(cache_key, version, rendered, tarball)`. Extend
   the cached build to carry the contract tarball too, built in the same critical section
   from the **same rendered tree**. Do not add a second cache or a second lock.
   `_build_tarball` grows a `root` parameter (default `TARBALL_ROOT`) and a member filter,
   so the contract tarball is the same deterministic packer — fixed member mtimes, zeroed
   gzip header, `0o755` for `.py`, sorted members — reused, not copied.
3. `get_contract_version()` — read `CONTRACT_VERSION` out of the rendered tree, strip. If
   the member is absent or empty, this is a build defect: raise the same 503 the missing
   snapshot raises, rather than serving a contract with no version.
4. `get_contract_version_payload()` = `get_version_payload()` plus `"contract_version"`.
   That gives D9's envelope (`contract_version`, `kit_version`, `platform_url`,
   `instance_name`, and the rest of the existing keys) for free, and keeps one payload
   builder.
5. `get_versioned_contract_tarball()` → `(kit_version, bytes)`, mirroring
   `get_versioned_tarball`.
6. Routes — two new handlers on the **existing** `start_router`, so both the
   `/agent-start` and `/api/agent-start` mounts get them with no extra registration, and
   the router-level `_rate_limit_guard` + `_enabled_guard` dependencies apply unchanged:

| Method | Path | Response | Representation string |
|---|---|---|---|
| `GET` | `/contract.tar.gz` | `application/tar+gzip`, `Content-Disposition: attachment; filename="cinna-contract.tar.gz"` | `"contract.tar.gz"` |
| `GET` | `/contract/version` | JSON, D9 envelope | `"contract/version"` |

   Both go through `_not_modified` / `_kit_headers` exactly as `get_kit_tarball` and
   `get_kit_version` do. **Do not fork the response plumbing.**

7. **ETag/`X-Kit-Version` decision, and it matters:** both new representations key on
   **`kit_version`** (the content hash), *not* `contract_version`. `contract_version` is
   hand-maintained and does **not** move when a template or the schema changes — an ETag
   keyed on it would tell a client "unchanged" after a real contract edit. `kit_version`
   moves on any content change, which is exactly the invalidation semantics an ETag
   promises. Write this down in a comment; it is the kind of thing a later reader
   "simplifies".
8. Generalise `_parse_remote_version` (`kit.py:1196`) to take the key name — see Phase 5.
   Nothing server-side depends on it; this bullet is a cross-reference only.

**Route ordering:** register `/contract/version` and `/contract.tar.gz` **before** the
catch-all `@start_router.get("/kit/{path:path}")` — they do not collide (different
prefixes), but keep the file's existing "specific before catch-all" ordering.

**Reviewable when:** both paths answer on both mounts; 404 on a disabled instance; 304 on
`If-None-Match`; the tarball extracts to exactly one top-level `cinna-contract/`
directory holding `kit.json` + `layout.json` + `CONTRACT_VERSION` + `CHANGELOG.md` +
`schema/` + `templates/` and **no `VERSION`**, **no guides**, **no `tools/`**,
**no `README.md`**, **no `START.md`**, **no `assistants/`**.

---

## Phase 4 — `kit.py`: `layout.json` as the exclude source, and pattern semantics (D6)

**Goal:** one exclude list, one matcher, matching the desktop byte for byte. This phase
is where the §9.3 hash parity is won or lost.

**Files**

- **EDIT** `docs/local_agent_kit/tools/kit.py`

**Work**

1. Add `LAYOUT_PATH = KIT_DIR / "layout.json"` and `layout_config()` alongside
   `kit_config:227`. `cloud_import_excludes:234` reads
   `layout.json` → `cloud_import_excludes`, falling back to `DEFAULT_EXCLUDES` only when
   `layout.json` is missing or unusable.
2. Update `DEFAULT_EXCLUDES` (`kit.py:146`) to the **same content** as the shipped
   `layout.json` list. A fallback that diverges from the real list is a silent
   hash-mismatch generator.
3. **Rewrite `is_excluded:1304`** to the documented semantics. This is a port of
   `matchesPattern` in `/Users/evgenyl/dev/ml-llm/cinna-desktop/src/main/kit/layout.ts`
   and every detail below is load-bearing:
   - **Normalise both sides**: backslashes → `/`; strip one leading `./`; strip leading
     `/`; strip trailing `/`. Empty pattern or empty path → no match.
   - **Directory branch is selected by the RAW pattern's trailing `/`**, tested *before*
     normalisation (`pattern.rstrip()` ends with `/`). A directory pattern matches the
     directory itself **and** everything beneath it: for `end` in
     `len(pattern_segments) … len(path_segments)`, match `pattern_segments` against
     `path_segments[:end]`.
   - **Non-directory patterns are FULL-LENGTH matches** — the pattern must consume the
     whole path. This is what anchors `README.md` to the agent root so `docs/README.md`
     and `scripts/README.md` travel.
   - `**` matches **zero or more** segments (so `**/.env` covers a root-level `.env`).
   - Within one segment: `*` → `[^/]*`, `?` → `[^/]`, every other regex metacharacter
     escaped. Do **not** use `fnmatch` — its `*` crosses `/` and it has no `**`.
4. **Behaviour changes this rework causes, all intended, all to be listed in the CHANGELOG
   (Phase 10):**
   - `AGENTS.md` / `CLAUDE.md` are matched by **basename at any depth** today, so
     `knowledge/AGENTS.md` is dropped. After: root-anchored only — it travels.
   - `*.pyc` matched by basename today; the new list uses `**/*.pyc` — same effect,
     different mechanism.
   - `.gitkeep` is newly excluded (`**/.gitkeep`), so `files/.gitkeep` stops travelling
     and the cloud workspace gets no empty `files/` directory. **Verify this is acceptable
     before shipping** — if an empty `files/` is required cloud-side, say so and drop
     `.gitkeep` from the list on both sides.
   - `README.md` and `Makefile` at the agent root are newly excluded ("the platform
     provides its own"). **Verify:** the cloud `status_refresh_command` resolves through
     `docs/CLI_COMMANDS.yaml`, not through `make`, so dropping the Makefile should be
     inert — confirm against the env-core command runner before shipping, and record the
     confirmation here.
5. **Reconcile `ALWAYS_EXCLUDE` (`kit.py:137`) and `is_env_filename` (`kit.py:127`).**
   This is the correction from §1.4 and it is the single most important item in the phase:
   - `cmd_export:1370` appends `ALWAYS_EXCLUDE` to the loaded patterns. Keep the append —
     it is a real safety net against a tampered `layout.json` — but make it **provably a
     no-op against the shipped list**: every `ALWAYS_EXCLUDE` pattern must already be
     covered by `layout.json`'s list. Phase 12 asserts this with a test. If the append
     ever adds something, the exported set and the hashed set diverge and the desktop
     reports "unpublished changes" forever.
   - `cmd_export:1369` short-circuits on `is_env_filename(path.name)`, which drops
     **`.env.example`** (`".env.example".startswith(".env.")` is `True`). **Remove that
     short-circuit** and rely on the patterns; `**/*.env` (Phase 1, deviation b) covers
     `prod.env` and friends and does **not** match `.env.example`. Leave `is_env_filename`
     itself in place — `_validate_secrets:794` still uses it, correctly, for a different
     job.
6. `cmd_export:1385`'s error string mentions `kit.json cloud_import.exclude` — update it
   to name `layout.json`.

**Reviewable when:** a table-driven matcher test (Phase 12) passes for every pattern in
the shipped list, `ALWAYS_EXCLUDE` is provably redundant, and an export of the scaffold
produces the file set the desktop's `collectExportFiles` would produce for the same tree.

---

## Phase 5 — `kit.py`: `content_hash`, `export --hash`, `_parse_remote_version` (D7, §9.3)

**Files**

- **EDIT** `docs/local_agent_kit/tools/kit.py`

**Work**

1. `collect_export_files(agent_dir, patterns) -> list[str]` — the walk, ported from
   `exportTree.ts` `collectExportFiles`:
   - agent-root-relative POSIX paths, no leading `./`.
   - **Symlinks are never followed and never listed** — files *and* directories.
   - The exclude list is tested on **directories too, before descending**, so an excluded
     directory is never walked.
   - Dotfiles **are** walked; they are dropped by the patterns, not by the walk. This means
     the existing `iter_files:265` (which hard-skips `SKIP_DIRS`) is **not** the right walk
     — write a separate one, or the hash will differ from the desktop's for any agent that
     has a `.mypy_cache/` the patterns do not name.
2. `content_hash(agent_dir, patterns) -> str`:
   ```
   digest = sha256()
   for rel in sorted(files, key=lambda p: p.encode("utf-16-be")):
       try:    entry = sha256(path.read_bytes()).hexdigest()
       except: entry = "unreadable"
       digest.update(f"{rel}\0{entry}\n".encode("utf-8"))
   return "sha256:" + digest.hexdigest()
   ```
   - **The sort key is not cosmetic.** `key=lambda p: p.encode("utf-16-be")` reproduces
     JavaScript's default string sort (UTF-16 code-unit order) byte for byte. Plain
     `sorted()` agrees for ASCII and diverges for non-BMP characters in a filename. Ship
     the comment naming *why*: the only symptom of getting it wrong is "unpublished
     changes forever", which no one will trace back to a filename with an emoji in it.
   - Hex digests are lowercase; `hashlib.hexdigest()` already is.
   - Line format is exactly `<relpath>` + NUL + `<hexdigest>` + LF. Nothing else — no
     mtimes, sizes, modes, or directory entries.
   - An empty file list yields `sha256:` + the digest of empty input.
   - **Unreadable-file detail worth matching exactly:** the desktop substitutes its
     `UNREADABLE_MARKER`, which is the literal `\0unreadable` — so its line for an
     unreadable file contains **two** NULs (`rel` NUL NUL `unreadable` LF). Read
     `/Users/evgenyl/dev/ml-llm/cinna-desktop/src/main/kit/exportTree.ts` and match the
     byte sequence exactly; if it is two NULs, ours must emit two. Record the confirmed
     byte sequence in this plan when implementing.
3. **The hash is over the SOURCE tree, filtered — never over the export destination.**
   `cmd_export` mutates the destination (it regenerates `workspace_requirements.txt` and
   clears the `cloud` block), so hashing the destination would produce a number the
   desktop can never reproduce. Implement `--hash` against the source walk and assert this
   in a test.
4. `cmd_export` gains `--hash`: print the hash of the exported (source-filtered) tree.
   Print it in the summary unconditionally too, per D7.
5. `_parse_remote_version:1196` — add a `key` parameter (default preserving today's
   `("kit_version", "version")` order) so a contract-version poll reuses one parser rather
   than duplicating it. Nothing calls it with the new key yet; that is fine, the
   generalisation is the deliverable.
6. `cmd_export` currently clears `manifest["cloud"]` in the destination. Extend: migrate a
   `cloud` object into `publications[]` per §3, then clear both `cloud` and
   `publications[]` in the **exported copy** (other instances' URLs have no business in a
   cloud workspace). Keep the source manifest untouched — `export` has never written to
   the source and must not start.

**Reviewable when:** a table-driven `content_hash` test passes, including the empty-tree
case, a non-BMP filename, and an unreadable file; and `export --hash` on the scaffold
equals `content_hash` computed independently on the same source.

---

## Phase 6 — `kit.py new`: tokens, `--description`, `--json`, serialisation parity (§5, §9.1)

**Goal:** `kit.py new` and the desktop's New-agent flow produce byte-identical trees
except `id` and `created_at`.

**Files**

- **EDIT** `docs/local_agent_kit/tools/kit.py`
- **EDIT** `docs/local_agent_kit/templates/agent/cinna-agent.json`
- **EDIT** the 9 template files carrying `{{name}}` / `{{slug}}` (see below)

**Work**

1. **Token rename — the part A2 got wrong.** `substitute_tokens:671` substitutes
   **lowercase** `{{name}}` / `{{slug}}` today. Adopt the desktop's set
   (`MANIFEST_TOKENS` in `src/shared/kit/manifest.ts`): `SLUG`, `NAME`, `DESCRIPTION`,
   `ID`, `CONTRACT_VERSION`, `KIT_VERSION`, `CREATED_AT` — all `{{UPPER_SNAKE}}`.
   **PROVENANCE CAVEAT — carry this wherever the set is repeated; this line is the root
   the downstream copies were derived from, and its absence is why they drifted (§0 Bucket
   25).** The set is correct **as a set** and silent on the one member that behaves
   differently. `KIT_VERSION` is on the scaffolder's list **and** on the platform's, on
   purpose, so **neither list settles it — and shape settles nothing either, since both
   classes are `UPPER_SNAKE`.** What separates the classes is **provenance, not
   membership**: the platform substitutes `KIT_VERSION` across every file when it renders a
   kit for download; `kit.py new` fills it only in a kit that was **never** rendered. A
   downloaded kit is a rendered one, so **six of the seven** are what a reader actually sees
   waiting, and the scaffolder's seventh substitution finds nothing to do. The authority is
   `SCAFFOLD_TOKENS` in `kit.py` and the comment above it, which stated this correctly all
   along; **an edit to that comment is an edit to every doc derived from it.**
   Files to update (current lowercase-token occurrences):
   `templates/agent/README.md` (lines 2, 5, 9, 17 — line 2 *describes* the tokens, so its
   prose changes too), `templates/agent/AGENTS.md` (1, 7),
   `templates/agent/Makefile` (1), `templates/agent/pyproject.toml` (2, 4),
   `templates/agent/docs/WORKFLOW_PROMPT.md` (9), `templates/agent/config/README.md` (1),
   `templates/agent/knowledge/README.md` (1), `templates/agent/credentials/README.md` (1),
   `templates/agent/scripts/README.md` (1).
   **Watch the collision:** `LocalAgentKitService._render_bytes` substitutes a fixed
   platform token set that already includes `KIT_VERSION`, `PLATFORM_URL`, `INSTANCE_NAME`
   and friends. `NAME` is **not** in that set and must not be added — but the existing test
   `test_scaffold_placeholders_are_left_for_kit_py` asserts the lowercase pair survives
   rendering; it will need updating, and its *intent* (platform must not eat scaffold
   tokens) must be preserved for the new uppercase names. The
   `PLATFORM_TOKEN_RE = ^\{\{[A-Z][A-Z0-9_]*\}\}$` heuristic in
   `backend/tests/unit/test_local_kit_tool.py:76` no longer distinguishes the two classes
   once scaffold tokens are uppercase — replace the heuristic with an explicit name list.
   **This is the sharpest edge in the phase.**
2. `templates/agent/cinna-agent.json` — replace with the desktop's authored file
   verbatim, **key order included** (the writer preserves insertion order, so key order is
   part of the byte-identity contract):
   `contract_version`, `id`, `kit_version`, `created_at`, `name`, `slug`, `description`,
   `example_prompts`, `router_trigger_prompt`, `prompts`, `runtime`,
   `status_refresh_command`, `credentials`, `schedules`, `handovers`, `features`,
   `publications`.
   Note two content changes this brings: `status_refresh_command` becomes `null` (ours is
   `"/run:status"`) and `cloud` is replaced by `publications: []`. **Keep `"/run:status"`
   if we want the scaffold to ship a working status command** — but then the desktop's
   template diverges and D5 says ours wins, so the answer-back must name it. Decide
   explicitly; do not let it fall out of a copy-paste.
   Also: `template_description()` (`kit.py:918`) reads this file's `description`, which
   becomes `{{DESCRIPTION}}`. K2's "still the scaffold placeholder" check must therefore
   compare against the *substituted default*, not the raw token — fix it in the same change
   or the check silently stops firing.
3. `cmd_new:708` fills all seven tokens: `NAME`, `SLUG`, `DESCRIPTION` (from
   `--description`, defaulting to today's template sentence), `ID` (fresh UUID v4,
   lowercase hex, hyphenated — `str(uuid.uuid4())`), `CREATED_AT` (ISO 8601, UTC),
   `CONTRACT_VERSION` (from the contract in use: `kit.json.contract_version`, falling back
   to the `CONTRACT_VERSION` file), `KIT_VERSION` (from `kit_version():217`).
   Stop patching `manifest["slug"]` / `["name"]` / `["kit_version"]` on the parsed dict —
   once the template carries tokens, `substitute_tokens` does the whole job and the dict
   patch would fight it. Keep the manifest re-write only for the serialisation guarantee in
   step 5.
4. `new_parser:1417` — add `--description "<sentence>"` and `--json`.
   `--json` emits the created agent's `path`, `slug`, `id` and `contract_version` on
   stdout **and nothing else** (no "Next steps" block), so a conformance harness can drive
   it without scraping.
5. **Serialisation parity.** The desktop writes
   `` `${JSON.stringify(manifest, null, 2)}\n` ``. Python's equivalent is
   `json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"` — which `cmd_new:748`
   already does. Assert all four properties in a test: 2-space indent, exactly one trailing
   newline, `sort_keys=False` (insertion order preserved), `ensure_ascii=False` (non-ASCII
   emitted raw, matching `JSON.stringify`). Python's default `separators` for indent mode
   is `(',', ': ')`, which already matches. **Unknown keys are preserved structurally** —
   nothing whitelists keys on either side; do not add a key filter.
6. `restore_scaffold_ignore_files:685` — **unchanged in intent**, and this is the first of
   the two traps. It must keep renaming the dotless files. `SCAFFOLD_IGNORE_FILES:102`
   should now be sourced from `layout.json` `scaffold_ignore_files.agent` rather than being
   hard-coded, so the contract stays the one truth — the desktop reads the same block.
   `templates/agent/credentials/.gitignore` stays **dotted** and stays out of that list.

**Reviewable when:** `kit.py new x --name "X" --description "Y" --json` emits the four
fields; the created manifest matches the desktop's key order and byte shape; no
`{{…}}` survives; the dotless→dotted restoration still happens for both agent ignore files
and `credentials/.gitignore` still has its dot.

---

## Phase 7 — `kit.py validate`: contract gate, new fields, severity parity (§5, §9.2, brief §4)

**Files**

- **EDIT** `docs/local_agent_kit/tools/kit.py`

**Work**

1. **Replace the `schema_version` gate with the `contract_version` gate.** Delete the
   `SUPPORTED_SCHEMA_VERSION` comparison in `validate_manifest:355` (kit.py:57, :369-380)
   and implement the §4 table, ported from
   `/Users/evgenyl/dev/ml-llm/cinna-desktop/src/shared/kit/contractVersion.ts`
   `checkContractCompatibility`:

| Folder vs. tool | Result |
|---|---|
| same major, any minor | pass silently |
| folder major **newer** | **error** — "refresh the kit", exit non-zero |
| folder major **older** | **warning** — migratable, run it |
| unparseable / absent | legacy path, see (2) |

   Semver pattern identical to the schema's; **only `major` affects the verdict**.
   The tool's own contract version comes from `kit.json.contract_version`, falling back to
   the `CONTRACT_VERSION` file — never from the network.
2. **Legacy exemption.** A manifest with `schema_version` and **neither**
   `contract_version` **nor** `id` is legacy: emit a **warning** with a re-stamp
   suggestion and skip the rest of the identity checks (mirroring `checkIdentity()`'s
   early return). Any other manifest must have both. `schema_version` is tolerated,
   non-`const`, non-required, and **nothing branches on its value**.
3. **New field checks:** `id` (UUID shape), `contract_version` (semver shape),
   `created_at` (string-or-null; note the desktop does **not** validate it — keep ours
   loose so we cannot be stricter than them), `runtime` (`model`/`credential` string or
   null, `permissions` an object), `publications[]` (`platform_url` + `agent_id` required
   and non-empty; the five optional fields string-or-null).
4. **`runtime.credential` secret-lookalike is an ERROR** (brief §4):
   `^(sk-|sk_|ghp_|gho_|xox[baprs]-|AIza|AKIA)` or longer than 200 characters →
   `manifest.runtime.credential_looks_like_secret`, with a rotate-it message. Mirrors the
   desktop exactly.
5. **Apply every demotion and addition from §0 Bucket 3.** Concretely:
   - Demote to warning: K1 (`_validate_requirements`), K4 (missing `.gitignore`),
     K5 (empty workflow prompt), K6 (unfilled `{{…}}` in the workflow prompt),
     K7 (`.env` tracked by git), S1 (duplicate credential slot name),
     S2 (unknown credential `type`), S3 (handover target missing).
   - Keep as an error and request upstream: K10 (forbidden value key in a credential slot).
   - Reconcile P1 (command-name pattern): error on the desktop's looser pattern, warn on
     our stricter convention.
   - Add: D-1 `/run:` reference resolution (**error**), D-2 unparseable catalog line
     (**error**), D-3 `example_prompts` bounds, D-4 self-handover, D-5 unroutable,
     D-6 STATUS.md frontmatter, D-7 non-boolean `features` member,
     D-8 missing `scripts/README.md`, D-9 deprecated `cloud` (info).
6. `cmd_validate:1017`'s `--json` payload gains `contract_version` (the folder's) and
   `tool_contract_version`, so a conformance harness can see the gate's inputs.
7. `--cloud-ready` keeps promoting the readiness set to errors. Document in the code that
   this promotion is **kit-only** (K13) and has no desktop analogue, so a future reader
   does not "align" it away.

**Reviewable when:** a legacy manifest (only `schema_version`) validates with a warning; a
manifest whose `contract_version` major is `2` fails with a refresh-the-kit error; a
`runtime.credential` of `sk-abc…` errors; and the demotion list above is complete —
checked by running `validate` over the deliberately-broken folder from §9.2 and diffing
the finding set against the desktop's.

---

## Phase 8 — `kit.py chat` (D10)

**Files**

- **EDIT** `docs/local_agent_kit/tools/kit.py`

**Work**

`kit.py chat Local/<slug> "<prompt>"`, standard library only (`urllib.request`):

1. Read `<agent>/app-data/desktop.json`. Missing file, missing or empty `api_base_url`,
   missing or empty `agent_token` → exit non-zero with **one line** saying the desktop is
   not connected. The path constant comes from `layout.json` `desktop_owned`
   (`app-data/desktop.json`), not a second hard-coded string.
2. `POST {api_base_url}{chat_path or "/chat"}` with `Authorization: Bearer <agent_token>`
   and body `{"prompt": "<text>"}`.
3. Response is newline-delimited JSON. Print each line's text as it arrives. **Be tolerant
   about the key**: `text`, then `content`, then `delta` — the desktop has not built this
   API yet, and this tolerance is the seam that spares us a second round-trip. A line with
   `"type": "error"` prints to stderr and sets a non-zero exit. End of stream with no error
   → exit 0.
4. Connection refused, 401, or any non-2xx → exit non-zero with a one-line explanation.
   **Never fall back to role-play** — that is the whole reason the verb exists.
5. Never print the token, and never print `desktop.json`'s contents.
6. `chat_parser` in `build_parser:1410`, positional `path` and `prompt`.

**Deliberate-impermanence note for the answer-back:** the tolerant key handling and the
`chat_path` default are provisional. Record them so they do not calcify.

**Reviewable when:** a stub loopback server exercises the happy path, the error-line path,
the 401 path and the connection-refused path, and each exits with the specified code.

---

## Phase 9 — `kit.py list`: DESKTOP column and `Cloud/<host>/` (§5, §6)

**Files**

- **EDIT** `docs/local_agent_kit/tools/kit.py`

**Work**

1. `cmd_list:1119` / `_print_table:1107` — add a **`DESKTOP`** column: whether the agent
   folder carries `app-data/desktop.json` with a usable `api_base_url` **and**
   `agent_token`, i.e. whether `kit.py chat` will work. Columns become
   `SLUG · NAME · RUNGS · CLOUD · DESKTOP`.
2. `_rungs_present:1062` — the `go_cloud` rung currently keys on
   `manifest["cloud"]["agent_id"]`. Extend to a non-empty `publications[]` **or** the
   legacy `cloud` object, so a folder stamped by either tool reports the rung.
   Same for `cmd_list`'s `CLOUD` cell.
3. **The `Cloud/<host>/` code change A6 surfaced.** `cmd_list` hard-codes
   `root/"Cloud"/".cinna"/"account.json"` and `root/"Cloud"/"agents"`. Rework to enumerate
   `root/<layout.workshop.cloud_dir>/*/` and report one block per instance host, each with
   its own `.cinna/account.json` and `agents/`. Keep reading the flat legacy layout when it
   exists, so an existing workshop does not silently stop being listed.
4. Take `cloud_dir` / `agents_dir` / `kit_dir` from `layout.json` `workshop`, not from
   string literals.

**Reviewable when:** `list` shows both columns; a workshop with `Cloud/acme.example.io/`
and `Cloud/other.example.io/` lists both; a legacy flat `Cloud/.cinna/` still lists.

---

## Phase 10 — Templates, guides, CHANGELOG, and the `~/Documents/CinnaAgents` sweep (§6, §7, brief §3)

**Files — kit content**

- `docs/local_agent_kit/START.md` (lines 20, 24, 28, 73)
- `docs/local_agent_kit/assistants/codex.md` (line 18)
- **NEW** `docs/local_agent_kit/assistants/cinna-desktop.md`
- `docs/local_agent_kit/README.md` (document index — add the new assistant note,
  `layout.json` and `CONTRACT_VERSION` to the Documents table; the `credentials/` row of
  the folder table stays "never copied to the cloud", per Bucket 2)
- `docs/local_agent_kit/CHANGELOG.md`
- `docs/local_agent_kit/guides/01-first-agent.md` (§2 scaffold command gains
  `--description`)
- `docs/local_agent_kit/guides/03-scripts-and-data.md` ("Where things go" gains the
  `app-data/desktop.json` paragraph)
- `docs/local_agent_kit/guides/05-schedules.md` (line 111)
- `docs/local_agent_kit/guides/10-testing-locally.md` (§1 role-switch test gains the
  "test through `kit.py chat` when the marker exists; role-play is the no-desktop
  fallback" rule; §2 says run **every** example prompt through `chat`)
- `docs/local_agent_kit/guides/11-go-cloud.md` (lines 61–76, 149, 154, 230 — `Cloud/<host>/`
  per-instance workspaces; line 66's path)
- `docs/local_agent_kit/guides/12-keeping-up-to-date.md` — **two separate edits, and the
  first is in ONE place, not two** (corrected in place; the original wording named "line ~46
  **and** the 'Kit version on an agent' section" for it): (a) the
  `schema_version`-is-breaking rule occurs **once**, in the "Read the changelog properly"
  list, and becomes **major-`contract_version`**-is-breaking; (b) separately, the "Kit version
  on an agent" section gains the contract's changelog and `/contract/version` alongside the
  kit's. Derive the line, do not trust one.
- `docs/local_agent_kit/templates/root/AGENTS.md` (the `chat` row in the commands table;
  the new "If this workshop is managed by Cinna Desktop" section between "## Commands" and
  "## Freshness"; the "## Cloud" section becomes per-instance `Cloud/<host>/`)
- `docs/local_agent_kit/templates/root/CLAUDE.md` (per-subfolder precedence: inside
  `Cloud/<host>/`, the CLI-generated `CLAUDE.md` wins)
- `docs/local_agent_kit/templates/root/README.md` (the `Cloud/` row)
- `docs/local_agent_kit/templates/root/gitignore` (add **both** `Cloud/.cinna/` **and**
  `Cloud/*/.cinna/` — a `*` segment cannot match an empty one, so the second does not cover
  the legacy flat workspace Phase 9 deliberately keeps working, and that workspace's
  `.cinna/account.json` holds an account token. Corrected from a single-pattern instruction;
  see §0 Bucket 26a for the reproduction. Also add `.cinna-kit/.last_refresh_check`)
- `docs/local_agent_kit/templates/agent/AGENTS.md` (the `app-data/desktop.json` read-only
  bullet under **`## Rules`** — corrected in place: this file has **no** "Non-negotiables"
  section, which is what the original wording named. The three real `Non-negotiable`
  headings are in `START.md`, `guides/10-testing-locally.md` and
  `templates/root/AGENTS.md`, none of which is this file.)

**Files — outside the kit, the A1 correction**

- `backend/app/services/cli/local_agent_kit_service.py` (module docstring, line 7)
- `frontend/src/components/Onboarding/GettingStartedModal.tsx` (line 224 — the "What it
  creates" tree literal; **a user-facing surface**)
- `.cinna-core-kit/scripts/check_docs_references.py` (line 285 comment)
- `docs/application/local_agent_kit/local_agent_kit.md` (lines 83, 205)
- `docs/application/local_agent_kit/local_agent_kit_tech.md` (line 338)
- Leave `docs/drafts/local-agent-kit_plan.md` alone — historical artefact.

**Work**

1. **D8:** every `~/Documents/MyAgents` → `~/Documents/CinnaAgents`.
   `GettingStartedModal.tsx` says `MyAgents/` without the `~/Documents` prefix — change it
   to `CinnaAgents/`.
   **The file lists above are this PHASE's inventory, not this step's replacement target:**
   most of those files contain no occurrence and belong to other steps. **Do not work from
   the list — derive the set** (`grep -rn "MyAgents" --exclude-dir=.git .`) and replace what
   is actually there, leaving the three deliberate exclusions alone: the generated
   `platform-knowledge-env` mirror (regenerated by Phase 11's sync, never hand-edited),
   `docs/drafts/local-agent-kit_plan.md` (historical artefact), and the two planning documents
   (where the old string is the subject). Executed: see §0 Bucket 23.
2. **`assistants/cinna-desktop.md` (new).** Notes for the desktop's in-app building mode:
   it is a sandboxed assistant with no terminal beyond the engine's bash tool; it must not
   run `kit.py refresh`; it tests through the local API. This file is deliberately **not**
   in the desktop's authored contract — it belongs with the guides, which the contract does
   not ship. Register it in `README.md`'s Documents table.
3. **CHANGELOG.md — BLOCKING, and it does not travel with the rest of this phase.** If
   Phase 10 is compressed, split or deferred for any reason, **this step comes out and runs
   standalone.** The reason it is blocking rather than untidy: `CHANGELOG.md` is a
   **contract tarball member** (`local_agent_kit_service.py:96`, `CONTRACT_MEMBERS` at
   `:110`), and it currently ships two false statements about the tool inside the same
   archive — "Unreleased — manifest `schema_version` 1", and "`kit.py` refuses a manifest
   whose `schema_version` is higher than the one it understands", which Phase 7 falsified by
   deleting `SUPPORTED_SCHEMA_VERSION`. Handing another team an artefact that misdescribes
   our own tool is worse than a stale internal document by the margin separating a wrong
   premise you inherit from one you write down and send. The missing Compatibility table is
   the same defect one turn further: `schema/cinna-agent.schema.json:10`'s `$comment` says
   the rule lives in three places and "if you change one, change all three", and it ships in
   the same tarball as a changelog carrying none of them. See §0 Bucket 16i.3.
   The work: **CHANGELOG.md** gains the §4 Compatibility table **verbatim** (three implementations
   key off it), plus the governing preamble sentence about major/minor. Add a 1.0.0-shaped
   entry in the existing Breaking / Added / Changed order covering: the
   `contract_version` gate replacing `schema_version`; the manifest `id`; `cloud` →
   `publications[]`; `layout.json`; `runtime`; `created_at`; `app-data/desktop.json`; the
   uppercase scaffold token set; the exclude-list move and every behaviour change from
   Phase 4 step 4; the `chat` verb. **Replace** the existing line "A bump of
   `schema_version` is always a Breaking entry" with the contract-major rule.
4. **Record the two D6 divergences from the desktop's authored contract in the CHANGELOG**
   so they are visible to anyone reading the shipped kit, not only to us:
   `credentials/` stays wholly excluded, and `**/*.env` is added.
5. **Trap 1 — scaffold ignore files.** Nothing in this phase may rename a dotless
   `gitignore` to `.gitignore` in the source tree. `templates/root/gitignore`,
   `templates/agent/gitignore` and `templates/agent/app-data/cache/gitignore` stay dotless;
   `templates/agent/credentials/.gitignore` stays dotted. A live `.gitignore` inside
   `docs/local_agent_kit/` hides scaffold content from *this* repository and therefore from
   the synced snapshot and the shipped tarball — the kit a fresh clone publishes would
   differ from the one that was tested.

---

## Phase 11 — Deployment and sync verification (brief §5)

**No code.** A checklist that must be executed and its results appended to §0.

**Sequencing — these are rules, not ordering assumptions. This is the most consequential
sequencing fact in the run.**

- **Phase 11 runs AFTER Phase 10.** Phase 10 edits kit content — templates, guides,
  `README.md`, and the blocking `CHANGELOG.md` rewrite. A sync performed before Phase 10
  mirrors content that Phase 10 then changes, so it is invalidated by the phase it
  preceded. There is no partial credit: the mirror either equals the tree or it is wrong.
- **Phase 12's test runs are only meaningful after Phase 11, and any content edit made
  during or after Phase 12 re-stales the mirror and requires ANOTHER sync.** Phase 11 is
  not "sync once and tick the box"; it is **sync LAST, and re-sync after every later
  content edit**. Any phase, fix round or review pass that ends with an edit to a file
  under `docs/local_agent_kit/` owes a sync before the next run that reads the mirror.

**The failure this prevents, stated as the failure rather than as the rule:** the next
person who fixes one template file after the sync ships a mirror that disagrees with the
tree — and because of the rung order below, that disagreement never surfaces as an error.
It surfaces as a confident green from `make test-backend`.

**Why the mirror is the default reader and not an odd configuration.** `_find_kit_dir`
(`backend/tests/unit/test_local_kit_tool.py`) resolves in three rungs:
`$LOCAL_AGENT_KIT_DIR` → the repo checkout `docs/local_agent_kit/` → the synced snapshot at
`backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/`. The
snapshot rung is reached only when `docs/local_agent_kit/kit.json` is unreachable from the
test file's parents — **which is exactly the backend container, where `make test-backend`
runs.** A stale mirror is therefore not a configuration a developer might stumble into; it
is **what the project's own standard test command reads by default.** §0 Bucket 5f states
the same rungs as a per-run trap and gives the `LOCAL_AGENT_KIT_DIR` override that escapes
it; Bucket 19's carry-forward records how stale the mirror actually was when last measured.

1. **`sync_platform_knowledge.py` is path-glob based — CONFIRMED at planning time, re-run
   the check after Phase 1/10 land.** `sync_local_agent_kit()` in
   `.cinna-core-kit/scripts/sync_platform_knowledge.py` walks
   `KIT_SOURCE.rglob("*")` and copies every regular non-symlink file that passes
   `_is_publishable(rel)` (a denylist: `KIT_DENY_NAMES` = `.DS_Store`/`Thumbs.db`,
   `KIT_DENY_SUFFIXES` = `.key/.pem/.p12/.pfx/.crt/.swp/.orig`, and any name containing
   `.env` that does not end in `.example`). **It is not an enumerated file list**, so
   `layout.json`, `CONTRACT_VERSION` and `assistants/cinna-desktop.md` ride automatically.
   Verify each of the three passes `_is_publishable` — none contains `.env` and none has a
   denied suffix, so all three should. Run `make sync-platform-knowledge` and confirm they
   appear under
   `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/`.
   **Confirm BOTH mirrors, not just this one.** The same single invocation runs three steps
   (`main()`: `[1/3] sync_docs()`, `[2/3] sync_api_reference()`, `[3/3]
   sync_local_agent_kit()`), and `docs/application/local_agent_kit/local_agent_kit.md` rides
   the **first**, landing under
   `…/knowledge/platform/application/local_agent_kit/` — not under `local-kit/`. A check
   that looks only at `local-kit/` passes while the other tree is stale. So verify the kit
   mirror **and** `…/knowledge/platform/application/local_agent_kit/local_agent_kit.md`.
   `local_agent_kit_tech.md` is **never** mirrored, by design — `sync_docs()` skips any name
   containing `_tech`; do not look for it and do not file its absence as a defect.
   (This was a verification gap only, not a live one: the sync itself already covers both.
   See §0 Bucket 23e.)
2. **The image snapshot `_read_snapshot` reads — CONFIRMED path-glob.**
   `LocalAgentKitService._read_snapshot` walks `kit_dir.rglob("*")` and reads every regular
   non-symlink file, skipping only `_SKIP_DIRS` (`__pycache__`, `.git`, `.pytest_cache`).
   New files ride automatically. **Re-verify after Phase 1** by fetching
   `/api/agent-start/kit/layout.json` and `/api/agent-start/kit/CONTRACT_VERSION` from a
   running instance — the failure mode this guards is "works locally, 404s deployed".
3. **Not gitignored — CONFIRMED at planning time.** `git check-ignore -v` on
   `docs/local_agent_kit/layout.json`, `docs/local_agent_kit/CONTRACT_VERSION` and
   `docs/local_agent_kit/assistants/cinna-desktop.md` matches nothing (exit 1). Re-run
   after the files actually exist — an untracked file is a file that never reaches the
   snapshot.
4. **Proxy paths — CONFIRMED.** `frontend/nginx.conf:113` is
   `location ~ ^/agent-start(/|$)` and `frontend/vite.config.ts:19` is
   `"^/agent-start(/|$)"`. Both regexes match `/agent-start/contract.tar.gz` and
   `/agent-start/contract/version`. **No proxy change is needed.** Confirm by request, not
   by reading, once Phase 3 lands.
5. **`check_docs_references.py` exemption — CONFIRMED.** The whole
   `docs/local_agent_kit/` directory is excluded (`check_docs_references.py:288`), so new
   kit markdown needs no annotation. Separately, the checker's `docs/CLI_COMMANDS.yaml`
   findings against this plan document are false positives, not to be "fixed" — see
   Phase 13 item 5.
6. **Context package.** `ContextPackageService` embeds
   `LocalAgentKitService.get_rendered_tree()` under `context/local-kit/`. The new files ride
   it automatically because it consumes the rendered tree, not a file list — confirm.
7. **Trap 2 restated as the gate for this phase:** a new kit file that does not ride *both*
   the sync and the snapshot works locally and 404s in a deployed instance. Both mechanisms
   are glob-based, so the risk is a `.gitignore` or a denylist hit, not an enumeration —
   check those two, specifically, for each new file.
8. **`tools/__pycache__/` — the premise below was WRONG, and the correction is the useful part.
   ~~`git check-ignore -v` matches nothing~~ / ~~a `.pyc` rides the sync into the snapshot~~.
   **Corrected in place, §0 Bucket 27c.** Neither half held:
   (a) both bytecode paths are now ignored — `.gitignore:43-44` covers the source tree,
   `backend/.gitignore:1` covers the mirror — the specific fix, landed;
   (b) **a `.pyc` inside a `__pycache__/` directory never rode the sync at all.**
   `KIT_SKIP_DIRS` already contains `__pycache__`
   (`.cinna-core-kit/scripts/sync_platform_knowledge.py:137`) and the walk applies it at `:202`,
   **before** `_is_publishable` (defined `:162`, called `:215`) is ever consulted. Do not repeat
   the ride-into-the-snapshot claim: it is a false explanation of correct code, the Bucket 24b
   shape, and it sends a reader looking for a path that cannot exist.
   **The gap the general fix actually closes** — adding `.pyc` to `KIT_DENY_SUFFIXES` — is a **bare
   `tools/kit.pyc` written beside its source**, which no directory rule catches, because it sits
   in no `__pycache__/`. The shipped comment at `:144-148` states exactly that and is the
   authority; read it rather than this line. It remains the fix that matters, for the reason
   originally given: `_is_publishable` is allow-by-default, so the same gap recurs for the next
   binary artefact left in that tree.
   **Not a serving defect either way:** `_read_snapshot`'s `_SKIP_DIRS` keeps `__pycache__` out
   of the served tarball, and the contract tarball excludes `tools/` entirely.
   **Provenance, so nobody files the artefact's return as a regression:** the test suite
   regenerates it, importing the kit by file location
   (`test_local_kit_tool.py:176`). `docs/` is not mounted into the backend container but
   `./backend/app:/app/app` is, so a container run compiles the **mirror's** copy onto the host
   tree while a host run compiles the source tree's — two files, different bytes, both expected,
   both ignored (§0 Bucket 27c has the verified chain).
   Whatever `.pyc` is sitting in either tree **stays** as-is until something else legitimately
   rewrites it; this run's standing rule forbids `git clean`, `git checkout --`, `git stash` and
   `git reset` in any case, and this task needs none of them.

---

## Phase 12 — Tests (brief §6)

**Files**

- **EDIT** `backend/tests/unit/test_local_kit_tool.py` (the `kit.py` surface — it drives
  `kit.py` as a subprocess with `sys.executable`, which genuinely exercises the
  stdlib-only constraint; extend it, do not create a parallel module)
- **EDIT** `backend/tests/api/cli/test_local_agent_kit.py` (the serving surface)

**`kit.py` unit tests**

1. **`is_excluded` pattern semantics — table-driven, mandatory.** One row per documented
   behaviour: root anchoring (`README.md` excludes `README.md`, not `docs/README.md` or
   `scripts/README.md`), directory subtree (`app-data/` matches `app-data` and
   `app-data/storage/x`), `**` across segments **including zero segments**
   (`**/.env` matches `.env`), `*` and `?` within a segment only, backslash and `./`
   normalisation, and the raw-pattern trailing-slash detection. This fails silently
   otherwise.
2. **`content_hash` — table-driven, mandatory.** Empty tree; one file; two files whose
   sort order differs between plain `sorted()` and UTF-16 order (a non-BMP filename); an
   unreadable file; a file inside an excluded directory (must not appear); a symlink (must
   not be followed and must not be listed). Assert the exact `sha256:<hex>` string against
   an independently computed value, not against the implementation.
3. **`ALWAYS_EXCLUDE` is redundant.** Assert every `ALWAYS_EXCLUDE` pattern is already
   covered by the shipped `layout.json` list — i.e. the append at `cmd_export` changes no
   file's fate. This is the guard that keeps the exported set and the hashed set identical.
4. **`.env.example` travels; `prod.env` does not.** The two halves of §1 correction 4/5.
5. **`DEFAULT_EXCLUDES` equals `layout.json`'s list.** A diverging fallback is a silent
   hash-mismatch generator.
6. **Version authority.** `CONTRACT_VERSION`, `kit.json.contract_version` and
   `layout.json.contract_version` all read `1.0.0`.
7. **Manifest serialisation parity.** 2-space indent, exactly one trailing newline,
   insertion order preserved, `ensure_ascii=False`, unknown keys survive a read/write
   round trip.
8. **`new --json`** emits `path`, `slug`, `id`, `contract_version`; two runs produce
   different `id` and `created_at` and **identical** everything else (the §9.1 property,
   testable on our side alone).
9. **Contract gate.** Newer major → error + non-zero exit; older major → warning + zero
   exit; same major different minor → silent; legacy (`schema_version` only) → warning.
10. **`runtime.credential` secret lookalike** → error, for each prefix and for >200 chars.
11. **Every demotion in §0 Bucket 3** has a test asserting the *severity*, not just the
    finding. Severity agreement is the whole point of §9.2.
12. **`chat`** against a stub loopback server: happy path, `type: error` line, 401,
    connection refused, missing marker file — each with its exit code. The token-secrecy and
    token-egress assertions are **not** part of this item — they are blocking and are
    specified on their own below.
13. **`restore_scaffold_ignore_files` still restores both agent ignore files** and
    `credentials/.gitignore` keeps its dot — the existing
    `test_new_restores_the_dot_on_every_scaffold_ignore_file` and
    `test_the_kit_ships_no_ignore_rule_that_hides_its_own_content` must keep passing
    unchanged. If either needs editing, that is a signal the trap was tripped.
14. **Update, do not delete,** `test_scaffold_placeholders_are_left_for_kit_py` and the
    `PLATFORM_TOKEN_RE` heuristic in `test_local_kit_tool.py` — see Phase 6 step 1.
    **This enumeration was written before Phase 6 was final and was incomplete; it is
    amended here rather than trusted (§0 Bucket 22d/22e). Re-derive the set before starting
    — expect MORE than the four below, do not treat this list as closed.** Also:
    - **`test_no_unrendered_placeholder_survives`** (`tests/api/cli/test_local_agent_kit.py`)
      — its `_UNRENDERED_TOKEN` regex matches `{{UPPER_SNAKE}}`, which the templates now
      carry (`{{NAME}}`, `{{SLUG}}`, `{{DESCRIPTION}}`, `{{ID}}`, `{{CREATED_AT}}`,
      `{{CONTRACT_VERSION}}` — none of them server-rendered). **Its module comment still
      claims the scaffold tokens are lowercase; fix the comment as well as the scan**, or the
      next reader will believe it.
    - **`test_version_payload_matches_the_index`** (same file) — asserts
      `payload["schema_version"] == index["schema_version"]`, the coupling **D17** dissolves;
      it now raises on both sides. **Second independent breakage in that one file.**
    - **`PLATFORM_TOKEN_RE` goes VACUOUS rather than failing**: it exempts UPPER tokens from
      the leftover-scaffold-token check on the pre-Phase-6 premise that UPPER means
      platform-rendered. After Phase 6 an unsubstituted `{{NAME}}` would pass it. A guard that
      stops guarding without failing — fix it even though it is green.
    **Ordering hazard, and it costs a day if it is not read first:** every one of these is
    **invisible in a container run against the stale mirror**, which still holds the lowercase
    tokens and the old `/version` shape. Run the suite before Phase 11's sync and it is green;
    run it after and it goes red; the natural conclusion is *the sync broke it*, and the
    action that follows — reverting the sync — restores the green **and leaves the real
    defects in place.** The sync exposes these; it does not cause them.
    **Not part of this item:** `test_new_scaffolds_and_validate_exits_zero`'s
    `KeyError: 'schema_version'` reads the **manifest's** field (a Phase 7 consequence,
    already listed among Bucket 14's four survivors). The manifest's `schema_version` is live
    and stays; only `kit.json`'s was removed.

**Token-secrecy and token-egress — BLOCKING, and these do not travel with the rest of this
phase.** If Phase 12 is compressed, split or deferred for any reason, **these come out and run
standalone.** They are not trimmed with the rest.

The reason they are blocking rather than thorough: **every other item in this phase catches a
regression in behaviour, and these catch a regression that leaks a live credential.** A
behaviour regression announces itself — a wrong answer, a wrong exit code, a wrong file on
disk. This one is **silent by construction**: the leaking run is the one that looks like it
worked. Phase 8 closed two token-egress paths and proved each load-bearing by a reverted-copy
differential, but **its evidence harness is not in the tree** — no committed test exercises
`chat` anywhere (§0 Bucket 20b) — so the two most valuable findings of that run are protected
today by nothing but the code happening to be correct. **A guard with no committed test is a
guard someone deletes during a refactor with a green suite.**

**Write them as the differential that was actually run, not as a happy-path assertion.** Two
runs per guard against the same stub topology — a recording foreign host and a recording proxy,
a real state file holding a marker token (the shape §0 Bucket 20's method section describes):

- guard removed ⇒ **the foreign host / the proxy receives the bearer token**;
- guard present ⇒ **exit non-zero, and nothing is sent** — the recording host receives no
  request at all.

State the reason for the differential wherever these are written up, because it is what stops
someone simplifying them back into an assertion: **a test that only asserts the current code
path passes proves nothing about the default it exists to defend against.** The defaults in
question are `urllib`'s `HTTPRedirectHandler` copying `Authorization` across a cross-host 302,
and a default opener honouring `http_proxy` / `all_proxy` on a loopback call. A test that
drives `chat` at a well-behaved stub and checks the answer passes **identically** with
`_NoChatRedirect()` and `ProxyHandler({})` deleted from the opener — which is exactly the edit
it exists to stop.

The three assertions owed a committed home (§0 Bucket 20b), all over **combined stdout+stderr**:
the **token** never appears; **no other `desktop.json` value** appears; and **the `api_base_url`
itself** never appears — the last because D3 says a tool never prints the state file's contents
and the URL *is* contents. The third is the one a reader drops: implement "never print the
token" alone and the URL goes out in the next error line with the rule believed kept.

**Carry-forward cases ruled in §0 — these are required, not optional**

C1. **`test_export_excludes_local_only_paths_and_clears_the_cloud_block` must be fixed AND
    renamed.** It fails after R4 because it asserts the export-time manifest rewrite that
    R4 constraint 1 forbids: it writes a populated `cloud` block into the source manifest and
    then asserts `exported["cloud"] == {"platform_url": None, "agent_id": None,
    "imported_at": None}`. Export no longer writes the manifest at all (Bucket 16a), so the
    assertion must become the opposite one — the exported manifest is **byte-identical to the
    source**. The rename is not cosmetic and is owned by this phase too: a test named
    `..._clears_the_cloud_block` that no longer clears anything sends a future reader looking
    for behaviour we deliberately removed, and they will find the removal comment in
    `cmd_export` and have to reconcile two artefacts that disagree.
C2. **The cloud-absent export branch** (Bucket 13). Assert both that `cloud` is **absent**
    from the export and that the exported **key order is unchanged**. The order half is the
    one that catches a regression reintroducing the key somewhere other than the end, which
    is exactly what F6 did; an absence assertion alone would have passed against a
    correctly-ordered reinsertion.
C3. **Audit the rest of `test_local_kit_tool.py` for the same single-branch shape.** C1's
    test could only ever exercise the branch F6 did *not* live in, because it wrote the
    `cloud` key in as setup. A test named after a defect that structurally cannot reach that
    defect is worse than no test: it consumes the attention a real case would have got. Check
    every test whose name names a behaviour for whether its setup forecloses the failing
    branch.
C4. **`content_hash` is computed from the SOURCE, before any copy** (Bucket 15e.3). There is
    no such assertion today — `grep -n content_hash backend/tests/unit/test_local_kit_tool.py`
    returns nothing — so the single property that makes our digest agree with the desktop's
    holds only because the code happens to be written that way, and a later tidy of
    `cmd_export` can move the digest below the copy with nothing objecting.
C5. **`publications.json` cases** (Bucket 16): the migration moves `content_hash` exactly once
    and is then stable; an unreadable ledger makes `write_manifest` raise and write **neither**
    file; a write that absorbs nothing leaves a hand-edited ledger byte-unchanged; a `cloud`
    or `publications` value this build cannot read is left in place; and the manifest-side
    `publications` finding is a **warning** while the `publications.json` entry findings are
    **errors** (severity asserted, per item 11's rule).

**Serving-surface tests**

15. `/contract.tar.gz` and `/contract/version` on **both** mounts; 404 on a disabled
    instance; 429 under the rate limiter; `If-None-Match` → 304; per-representation ETags
    (the contract tarball's validator must differ from `kit.tar.gz`'s).
16. The contract tarball has **exactly one** top-level directory, `cinna-contract/`.
17. Membership: it contains `kit.json`, `layout.json`, `CONTRACT_VERSION`, `CHANGELOG.md`,
    `schema/**`, `templates/**` — and **no** `VERSION`, `START.md`, `README.md`, `guides/`,
    `assistants/`, `tools/`.
18. **Every declared contract member exists in the rendered tree.** A rename that silently
    ships a contract without `templates/` is the failure this catches.
19. Contract tarball bytes are deterministic across builds (the existing
    `test_tarball_bytes_are_deterministic` pattern).
20. `/contract/version` payload carries `contract_version` **and** `kit_version`, and its
    `contract_version` equals the `CONTRACT_VERSION` member.
21. `X-Kit-Version` on the contract responses is the **kit** version, not the contract
    version (Phase 3 step 7).

**Regression scope**

```
docker compose exec backend python -m pytest tests/api/cli/test_local_agent_kit.py -v
docker compose exec backend python -m pytest tests/unit/test_local_kit_tool.py -v
docker compose exec backend python -m pytest tests/api/ -k "local_agent_kit or agent_start" -v
```

Do **not** run the desktop-auth or account-CLI groups for this work — nothing here touches
them, and this plan forbids modifying their files.

---

## Phase 13 — Documentation

**Files**

- `docs/README.md` line 208 (the `local_agent_kit` row)
- `docs/application/local_agent_kit/local_agent_kit.md`
- `docs/application/local_agent_kit/local_agent_kit_tech.md`

**Work**

1. `docs/README.md` row: add the contract/guides split, the two new endpoints,
   `layout.json` + `CONTRACT_VERSION` as contract members, `contract_version` as the
   compatibility gate, and the `chat` verb. Keep it one row, keep the existing density.
2. Business doc: the contract-vs-guides split and who consumes each; the three version
   numbers and what each answers; the compatibility gate; the `Cloud/<host>/` per-instance
   model; `app-data/desktop.json` as a desktop-owned file with two frozen keys; the D6
   divergence and its reason.
3. Tech doc: new rows in the **Endpoints** table (§ "Public Routes"); `CONTRACT_MEMBERS`,
   `CONTRACT_TARBALL_ROOT` and the cached build under **Service** — **the `_KitBuild`
   NamedTuple, not a tuple**: document the shape that landed, because the code rejected the
   positional form on purpose (§0 Bucket 26f); the ETag
   decision from Phase 3 step 7; `layout.json` and `CONTRACT_VERSION` in **Kit content**;
   the reworked `is_excluded` semantics and `content_hash` under **`kit.py`**; the new
   `kit.py` verbs; the `~/Documents/CinnaAgents` path.
4. **Do not** write `docs/local_agent_kit/desktop_contract_answers.md` — out of scope; a
   separate run consumes §0.
5. **False positives from `check_docs_references.py` — do NOT "fix."** Running the checker
   over this kit's documents reports `docs/CLI_COMMANDS.yaml` broken at three locations. The
   path is **agent-folder-relative** — it means `docs/CLI_COMMANDS.yaml` inside a scaffolded
   agent folder, not a repo-root path — so it can never resolve against this repository and
   is correctly reported broken forever; leave it. (Cross-referenced from Phase 11 item 5.)
   The checker's other broken references in the plan documents are forward pointers to files
   not yet written — `desktop_contract_answers.md` (a separate run's deliverable,
   deliberately out of scope, see item 4 above) and `assistants/cinna-desktop.md` (created
   by Phase 10) — likewise not to be "fixed" by removing the reference.

---

## Phase ordering and dependencies

```
1  Contract data (layout.json, CONTRACT_VERSION, kit.json)
   ├─→ 2  Schema
   ├─→ 3  Backend endpoints            (needs only the files from 1)
   └─→ 4  kit.py excludes + matcher
          └─→ 5  content_hash, export --hash        (needs 4's matcher)
   2 ─→ 6  kit.py new: tokens, --description, --json
          └─→ 7  kit.py validate: gate + fields + severities
   1 ─→ 8  kit.py chat                  (needs layout.json desktop_owned)
   1 ─→ 9  kit.py list: DESKTOP + Cloud/<host>
6,8,9 ─→ 10 Templates, guides, CHANGELOG, CinnaAgents sweep
   1,3,10 ─→ 11 Deployment/sync verification
   all ─→ 12 Tests
   all ─→ 13 Docs
```

No phase depends on a later one. Phases 3, 4, 8 and 9 can run in parallel once 1 lands.

---

## Standing checklist (applies to every phase)

- [ ] Never run `git stash`, `git checkout --`, `git reset`, `git clean`, or anything else
      that discards working-tree state. The tree carries uncommitted work from other
      features. If one seems necessary, stop and report.
- [ ] Never write, edit or run anything inside `/Users/evgenyl/dev/ml-llm/cinna-desktop/`.
- [ ] Never touch `backend/app/api/routes/desktop_auth.py`,
      `backend/app/services/desktop_auth/`, or `backend/app/api/routes/cli.py`.
      **Scope, carried down from the opening "Out of scope" list so that it reads
      correctly here — thousands of lines from the heading that qualifies it, which is
      where this bullet is actually read. It binds every phase of THIS plan, at full
      force, and nothing has relaxed it. And it is a rule about what this plan's phases
      may WRITE, not a claim about what those files CONTAIN.** Other work reaches them
      legitimately, most specifically handover §8.2
      (`POST /api/v1/cli/account/desktop-token`, brief D12) — which that same opening
      list places outside this plan and hands to a **separate run**, and which
      necessarily lands in `routes/cli.py` and on the desktop-auth surface. §8.2 is not
      one of this plan's phases, so the prohibition never governed it. **Finding those
      files edited, or finding that endpoint inside them, therefore says nothing about
      this bullet**: it is not a breach, not an exception, and not evidence that the rule
      was overtaken. That work is settled at **D12** in
      `docs/plans/local_agent_kit_desktop_contract_requirements.md` and documented at
      `docs/application/desktop_auth/desktop_auth.md` and its `_tech` sibling — read
      those before drawing any conclusion here, rather than searching for it.
- [ ] Scaffold ignore files ship **dotless** (`gitignore`); `templates/agent/credentials/.gitignore`
      is the one deliberate dotted exception.
- [ ] Any new kit file must ride both `sync_platform_knowledge.py` and `_read_snapshot`,
      and must not be gitignored — verify per Phase 11, do not assume.
- [ ] Every verified fact goes into **§0 Answer-back findings** as it is established.
      A bucket that stays TODO at the end blocks the answer-back document.
- [ ] **Never quote a figure forward across a bucket boundary — re-derive it where you use
      it.** This covers *any* number, so nobody has to decide whether theirs qualifies:
      pattern and entry counts, test tallies, member counts, file:line citations, and
      ordinals ("the fifth required change", "the seventh instrument failure"). The
      mechanism is worth stating, because this has failed at four different scales inside
      one run and nobody was being careless: **a verified figure quoted forward becomes an
      unverified claim without anyone noticing the transition — the number was true when it
      was written, the quotation is faithful, and the falsity enters at the copy, with no
      step at which anyone did anything wrong.** So: a figure is true **as of its own
      bucket**. Re-derive it at the point of reuse; if you cannot, cite the bucket and say
      what it was *as of then* rather than restating it as current.
      **The same mechanism one level up, and it is not a figure: a phase that ENUMERATES
      SPECIFIC ARTEFACTS — named tests, named files, named call sites — goes stale exactly
      the way a quoted number does.** The enumeration was complete when written, it is
      faithfully copied forward, and the falsity enters when the code it enumerates moves
      underneath it. Nobody is careless: the list was written before the change that
      invalidates it existed in its final form. Instance, live in this document: Phase 12
      item 14 enumerated the tests to update for Phase 6's uppercase-token change and named
      **two of the three** — `test_no_unrendered_placeholder_survives` was missing, and
      Phase 6 was not yet final when that item was written (§0 Bucket 22c; the item has
      since been amended, and a fourth test was found that no phase list named at all).
      **So a phase's file or test list is re-derived before it is executed, never trusted as
      an inventory** — the phase says what to look for, and the executor finds the current
      set.
      **And the same failure with no number and no list in it: a CHARACTERISATION quoted
      forward.** "The matching literal in the backend's fallback payload" was how a brief
      described D17's second site; it is not a fallback, it is `_version_payload`, the live
      envelope of `GET /agent-start/version` (§0 Bucket 22f). The description was faithful to
      what its writer believed, it was accurate enough to pass, and the falsity entered at the
      copy — the stale-figure mechanism operating on prose. It matters because the two
      characterisations differ **to a reader**: one is an edit to a file they bundle, the other
      is a change to a public response shape they may poll. **So re-derive what a site IS, not
      only how many of them there are** — read the function before repeating a sentence about
      what it does. Evidence from this run
      alone: the exclude count corrected 40 → 41 in two places (Bucket 9f, Bucket 10, both
      pointing at 16h); three citation line-ranges handed to a recorder that did not check
      out; the required-desktop-changes ordinal that went "fifth" → "sixth" → "seven", each
      wrong before the next section landed; and an instrument-failure ordinal asserted as
      "seventh" that a later reader found was probably double-counting Bucket 14g.
      **The same rule with a much shorter half-life: a line-level claim about a file under
      CONCURRENT EDIT is stale the moment it is written.** Not stale by the next bucket — stale
      by the next save, made by someone else, while you are typing the citation. Evidence from
      this run: the coordinator made three location claims in briefs, and **two were wrong** —
      `cmd_export` named as the raise site where it was `_validate_files`, and a
      `manifest.ts:122` citation that was `desktopStateService.ts:121` — both about files
      another agent was editing at the time. A live demonstration is sitting in this document:
      Bucket 20e cites that raise site as `kit.py:2026`, and `_validate_files` now begins at
      `kit.py:2087`, with the `is_file()` check at `:2104`. The bucket was right when written
      and is unusable now, which is the rule rather than an exception to it. **The durable fix
      is not more care with line numbers — it is the cite-don't-restate rule below: briefs name
      the finding and require the implementer to locate it.** A brief carrying no line number
      cannot carry a stale one.
- [ ] **An implementation brief must NOT restate a finding's mechanism. It cites the finding
      and requires the implementer to reproduce it.** The wording to use is literally *"reproduce
      finding X and tell me if it does not hold as described"* — a pointer plus an obligation,
      never a retelling. **A brief that carries no mechanism cannot carry a stale one**, and
      that is the entire justification: the property is bought by the artefact's shape, not by
      the briefer's diligence.
      **Always add the corollary ask: "reproduce every finding yourself and tell me if any
      OTHER one fails to hold as described."** Two mechanisms in this run were wrong, and the
      assumption that a third is wrong too is what catches it. An implementer who checks only
      the one they were warned about will not find the next one.
      **Why the softer rule was insufficient — this is the transferable half, so do not trim
      it.** This document already carries the rule that a verified figure quoted forward
      becomes an unverified claim (the stale-figure bullet above). **The coordinator wrote
      that rule and then broke it within hours, in an implementation brief** — restating
      HIGH 3's trigger as HIGH 2's, which would have produced a passing reproduction of the
      wrong defect (Bucket 18). That is the demonstration that the **rule-as-intention is
      unenforceable**: it asks someone to notice that they are copying rather than deriving,
      at the exact moment copying *feels like faithful transmission*. Nobody experiences
      themselves as violating it. So enforcement moves out of the person and **into the
      artefact**: a brief with no mechanism in it has nothing to be stale.
      **This is the SAME MOVE as making `write_manifest` the sole writer of the manifest**
      (Bucket 16b) — turning a rule someone must remember ("remember to migrate the ledger
      keys") into a property of the structure ("there is one writer, and it migrates"). State
      the parallel explicitly wherever either is taught; it is the general technique this run
      keeps rediscovering, and it is what separates a control that holds from one that merely
      documents an intention. Related, same family, different level: the honest reading in the
      shapes section of what a review-checklist entry buys — **detection, not prevention**.
      A structural control prevents; a heuristic detects; an intention does neither reliably.
- [ ] **A brief must specify a reproduction as a concrete literal, not a prose description.**
      Evidence: the fix brief that broke the cite-don't-restate rule above was internally
      inconsistent — its prose named one trigger shape while its code literal named another —
      and the implementer followed the literal, so the reproduction was correct **by
      accident**. A literal is checkable and executable where prose is neither; here it is
      what saved a brief its own prose would have misled.
- [ ] **An implementer's account of what it built is unverified until the artefact is
      checked** — read the code, not the report describing it. **The corollary is the sharp
      half: the highest-risk claims in such a report are the ones describing PROTECTIONS,
      because nothing else will ever exercise them.** A behaviour claim gets a second opinion
      from the next test run or the next caller. A claim that a comment, note, guard or
      warning was added to stop a *future* editor doing the wrong thing has no second reader
      at all; if it is absent, the only thing that ever fails is the thing it was meant to
      prevent, later, silently, in someone else's turn.
      **Evidence, from this run (§0 Bucket 19j).** A fix round reported *"Both sites carry a
      note so nobody 'harmonises' them"*, describing a comment meant to stop a future editor
      reversing one of two deliberately opposed fail-safes. **The note does not exist.**
      `contract_exclude_patterns` and `secret_file_rules` each document their own fail
      direction well, and neither names the other — so the pair still reads as an
      inconsistency to anyone meeting it cold, which is exactly the harmonising edit the
      claimed note was supposed to forestall. The same report also gave a `read_text` caller
      count and a `JSONDecodeError` handler count, and both figures were wrong while both of
      the substantive claims they supported held on inspection. That combination is the
      usual one, and it is why such a report cannot be triaged by plausibility: the wrong
      parts sit inside a report that is otherwise right, written by someone who was not being
      careless.
      **The rule is not confined to reports, and not confined to protections — that is where
      it was first met, not where it lives.** It covers **any explanation, in a report or in
      the source, of why something is the way it is**: a docstring's justification for a
      fallback, a comment's account of what an archive contains, a note explaining why two
      neighbours differ. Second instance, in the source rather than in a report:
      `contract_version()`'s docstring justified its fallback by claiming a contract tarball
      "ships no `kit.json`" — false, since `INDEX_MEMBER = "kit.json"` is one of
      `CONTRACT_MEMBERS` and D2 says so. **The code was correct and only its stated reason was
      wrong**, so nothing could ever fail and the sole casualty was the next reader's belief
      (§0 Bucket 24b; the shapes section carries the sendable version). **So: verify the
      REASON, not only the behaviour** — a correct mechanism with a false rationale attached
      passes every test there is.
      **Why this class outranks every other reporting error, stated as a mechanism rather
      than as a severity.** **An unwritten protection leaves a reader neutral; a falsely
      asserted one moves them from neutral to wrong.** A reader told nothing about the opposed
      pair meets it cold and is at least uncertain — uncertainty is what sends someone to read
      both functions. A reader told "both sites carry a note" stops looking, and harmonises
      them. That is the whole distance between an omission and a false protection claim, and it
      is why this class is not just one more inaccuracy.
      **The recurrence, and its state as of this writing — re-derived here, not quoted
      forward.** The same false claim was later made a **second time, by an independent
      writer, in the source itself**: `desktop_state_file()`'s docstring says of the two
      opposed readers that they "carry cross-reference notes naming each other and saying not
      to harmonise them" — a place where a reader will believe it in a way no plan document
      commands. **It is no longer false: the underlying gap has since been closed.**
      `grep -n "harmonis" docs/local_agent_kit/tools/kit.py` now returns **three** lines —
      `contract_exclude_patterns()` and `secret_file_rules()` each carry the cross-reference
      note naming the other, and `desktop_state_file()`'s observation about them is accurate.
      §0 Bucket 19j's finding, and Bucket 20c's "returns exactly one line", both describe a
      tree that no longer exists; they stand as written, being true as of their own buckets.
      **Re-run the grep before repeating any of this.**
      **The transferable half is the reading of the recurrence, not the recurrence.** **Two
      independent writers making the same false claim says the claim is *attractive*, not that
      either writer was careless.** A protection that *ought* to exist is easy to describe as
      existing: the sentence writes itself out of the design, and nothing in the act of writing
      it ever consults the file. So "be more careful" is not a remedy here — it would not have
      caught either instance, and it mislocates the fault in the writers.
      **The remedy has a shape, and it is the same move as everything else in this list: make
      the ARTEFACT answer, not the author.** A report claiming a protection must carry **the
      grep output that shows it**, not a restatement that it is there. A restatement is exactly
      as cheap to produce whether or not the note exists; a grep output is not.
      **Same family as the rules above, and the connective tissue is the transferable part.**
      A status marker or a progress line is a claim about *position*; an implementation
      report is a claim about *work*. Neither survives verification reliably, and both are
      checked the same way — against the artefact, never against the sentence describing it.
      Every other instrument failure in this document is a measurement of the wrong artefact,
      or a brief pointing at the wrong defect; this one is trusting **the report about the
      work instead of the work**. Same disease, one layer up.
      **The position half, stated so it cannot be softened: an agent's final streamed line is
      not a statement of where it stopped.** A progress message describes an intention at a
      moment; it is not a record of what was completed. **Instance:** during a pause, a
      recording agent's last streamed line read *"Now the standing checklist rule (Task 1)"* —
      which reads exactly as though it had stopped before starting that task. All four of its
      tasks had in fact landed; it had done Task 1 last. The coordinator checked the file
      rather than believing the line, and so did not report a landed rule as missing.
      **A second instance, at a much larger grain, and it is the one that generalises: a
      STATUS LIST OF WHAT IS OWED goes stale exactly like a figure or an enumeration does.**
      A handover's list of outstanding work overstated what remained — Phase 9's four steps
      were found **already implemented** when the phase was dispatched. **The cause is known
      and is not a mystery, so do not record it as one:** the team lead has claimed it — the
      list was written **from their own reports rather than from the tree**, across a session
      boundary, after several items had landed since they last looked. Nobody was careless.
      **The list was true when it was written, and the falsity entered when the tree moved
      underneath it** — the stale-figure mechanism exactly, with *position* as its subject
      instead of a number. A status list is a claim about the world at the moment it was
      written, and it has no expiry stamped on it.
      **The practice that follows is a rule, not a courtesy to anyone: verify each phase's
      premises against the tree before dispatching it, and never start from "this is owed."**
      That is what caught this one — the phase's agent verified by execution, found the steps
      already present, found a real defect in the landed code, and fixed that instead of
      re-implementing what was there.
      **And name the temptation, because it is strong and it looks like diligence: make NO
      authorship claim about who implemented them.** The whole feature is uncommitted, so git
      carries no authorship evidence, and **mtimes are not authorship evidence** — several
      agents share this tree and a recent mtime looks exactly like a fingerprint. The honest
      version — *"this was already done; the list was stale"* — is both true and more useful
      than any attribution, because the attribution answers a question nobody needs answered
      while the staleness answers the one that recurs.
      **The cost of getting that one wrong is specific, and worse than a wasted re-run:**
      believing the line sends a second agent to write a rule that is already there, and **two
      copies of a standing rule in one document is how a rule stops being read.** A duplicated
      rule is not harmless redundancy — it makes the document look unmaintained exactly where
      it most needs to be trusted. So the check runs in both directions: before adding a rule,
      grep for one that already says it, and **sharpen the existing entry in place rather than
      adding a second.**
      **This is a property of reports, not a criticism of the agents that write them**, and
      say so wherever the rule is applied: an implementer writing up a round it has just
      finished is the reader least able to see the gap between what it meant to land and what
      landed. A rule that reads as blame gets applied selectively — to the rounds someone
      already distrusts, which are not the rounds where it pays.
- [ ] **A mid-task ruling goes to the agent that OWNS the file the ruling touches. If that
      agent is not running, the ruling waits, or is queued as a fresh task — it is never
      handed to whichever agent is conveniently available.** An implementer holding a brief
      that forbids a file must never be the one asked to write it: that forces it to choose
      between two of the coordinator's own instructions, and whichever it picks, the
      coordinator has stopped being able to say which file has one writer.
      **Recorded because of where it came from.** One-writer-per-file was enforced rigorously
      on every agent for this entire feature, and the single violation was the coordinator's:
      a ruling was sent to an implementing agent whose brief forbade it from touching this
      plan document, describing that agent in the message as "the recorder", while the actual
      recorder was mid-run. **For a period two agents were writing this document
      concurrently.** No damage — verified afterwards: single copies of every rule, buckets
      intact — but only because the writes happened to fall in non-overlapping regions.
      **Luck, not design**, and a merge in one region would have silently lost a bucket.
      The implementer chose well (it grepped, found that an equivalent rule had landed under
      it, and sharpened in place instead of duplicating) — **but that is not a property anyone
      can rely on**, and it is the same reason the rules above move enforcement out of the
      person and into the artefact. It is worth writing down precisely because it arrived from
      the least expected direction: the control was sound, universally applied, and broken by
      the party applying it.
- [ ] Any phase that runs `kit.py` tests, and the final regression run, must state in its
      report **which mode it ran in** and **one piece of positive evidence that the
      working tree was actually exercised** — a test that fails when the edit is reverted,
      an assertion on a string that exists only in the new code, or naming the mechanism
      used to prove it.
      **The rule generalises past tests to every tool that reports by staying quiet: an
      absence of complaints is evidence only once you have shown the tool would have
      complained.** A clean `tsc --noEmit`, a clean linter, a grep that finds no bad pattern
      and a green suite all produce the identical output when the tool never looked at the
      file — a path excluded by a `tsconfig`, a stale container copy, a typo in the glob.
      Instance from this run: an agent proved a TypeScript typecheck non-vacuous by appending
      a deliberate type error, confirming it appeared in the output, restoring the file and
      diffing to prove the restore. That is the same instrument as the reverted-copy
      differential and the md5-compared container copy below — **provoke the complaint you
      expect, once, before trusting the silence.** Note the residue problem when reporting it:
      a probe whose artefact is deliberately restored leaves nothing to check afterwards, so
      say you are recording a *method* and not a checked artefact. `LOCAL_AGENT_KIT_DIR` is meaningful evidence only for `kit.py`'s own
      unit tests (`tests/unit/test_local_kit_tool.py::_find_kit_dir`) — the *service*,
      `platform_knowledge_assets.local_kit_dir()`, never reads that variable, so naming it
      for a backend-side phase proves nothing; name whatever mechanism you actually used
      instead (see §0 Bucket 8f for the exemplar). **For a replacement edit — one that
      changes existing text rather than adds new text — a positive probe alone is
      insufficient:** a grep for the new string also passes when the copy under test
      contains both the old and the new wording, which is exactly what a partial or failed
      `docker compose cp` leaves behind. Probe in **both** directions — confirm the new
      string is present **and** the old string is absent. Exemplar: the schema description
      fix probed `grep -c "secret_files rules in layout.json"` = 1 **and**
      `grep -c "cloud_import_excludes applied"` = 0.
      **Two ways the ABSENCE half returns a false failure, both hit inside this run (§0
      Bucket 21g), so treat a failing absence probe as a hit to read rather than a verdict.**
      (i) **The correcting text often quotes the wording it retires**, in order to forbid it —
      a comment that ends "do not describe this as *«the old phrasing»*" contains the old
      phrasing, so the absence probe finds it and reports the edit incomplete. The
      coordinator hit this while verifying an implementer's report that was accurate, with
      that accurate report in front of it. **Read the hit before believing the probe.**
      (ii) **Use an exact-substring test, not a multi-line `grep -F`**: `grep -F` counts per
      LINE, so a multi-line needle yields a pair of numbers that looks like a measurement and
      measures nothing. Prefer a one-line `python3 -c` substring check over file bytes.
      **(iii) An absence probe over PROSE must be run against text you have just READ, not text
      you have just WRITTEN.** Third instance, and the first one caught by the person who caused
      it. An implementer corrected a sentence of its own that a later ruling had falsified, its
      edit script's assertion **failed**, and its absence probe reported the file clean anyway —
      the probe pattern was `"not behind this gate"` while the file said `"**not** behind this
      gate"`, with the markdown emphasis sitting between the words. **A failed edit plus a blind
      probe is indistinguishable from a successful edit**, and that combination produces a
      confident green over an unmade change. It was caught only because a traceback's line number
      did not match the assertion that was expected to fail, so its author went back for ground
      truth instead of trusting the green.
      **The transferable half is not "be careful with greps" — it is that the probe author was
      probing for their own PARAPHRASE of the file rather than for the file.** Prose carries
      markup, line wrapping and punctuation that a remembered sentence does not, so a pattern
      written from memory is a different string from the one on disk, and every one of those
      differences fails silently in the reassuring direction. So: copy the needle out of the file
      you just read, never type it from what you believe the file says. This is the
      "provoke the complaint you expect" rule meeting the stale-characterisation rule, and
      **neither one alone would have caught it** — the probe ran, and the characterisation it
      encoded was the stale thing.
      **Prose is where this was first met, not where it lives.** The same defect runs on code
      and on document structure with no sentence involved, so state it generally: **the probe
      was scoped to the author's expectation of how the thing would be WORDED, or of WHERE it
      would LIVE, rather than to the thing itself** — and both halves fail silently in the
      reassuring direction, because a probe that looked in the wrong place is indistinguishable
      from one that looked in the right place and found nothing. Two instances with no prose in
      them. **(a) Wrong span.** A grep for a role gate was run over a route handler's own body
      and reported "no gate", while the claim under test covered the whole request path —
      dependency and service method included. Live in this tree: `mint_child_token` in
      `backend/app/api/routes/cli.py` calls no gate in its own body, and
      `AgentService.assert_can_build` sits one layer down inside
      `AccountCLIService.mint_child_token`. **A handler span looks identical whether or not a
      gate sits beneath it**, and the only gate-shaped thing inside that span is a docstring
      asserting one — which, by the protection rule above, is precisely what must never be
      counted as the gate. **(b) Wrong wording, right thing.** A probe for this document's
      files-no-phase-may-modify rule used the phrasing that one of its two sites happens to use
      and found a single site; the two sites state the same rule in different words. Probing on
      the invariant instead — the path strings the rule is *about* — found both.
      **So scope the probe to the artefact's stable identity, never to your expectation of its
      shape:** grep the whole path a claim covers, not the span you expect the answer to sit
      in; grep the identifier, path or literal that cannot be reworded, not the sentence you
      expect to be wrapped around it. A reader applying this to a grep over code must reach the
      same conclusion as one applying it to a grep over a sentence.
      **The copy is itself an instrument, and it has its own failure mode: `docker compose cp`
      NESTS when the destination already exists.** Copying into a `/tmp/<dir>` that is already
      there creates `/tmp/<dir>/local_agent_kit/` and leaves the top-level `kit.py` **stale**,
      so the run exercises the *previous* edit — which produced one confusing round with a
      spurious extra test failure. **The md5 comparison is what caught it**, and that is the
      argument for comparing rather than trusting the copy. The copy is therefore never one
      command:

      ```bash
      # `docker compose cp` NESTS into an existing destination — always clear it first.
      docker compose exec backend rm -rf /tmp/<dir>
      docker compose cp docs/local_agent_kit backend:/tmp/<dir>
      # Non-vacuity: this must equal `md5 -q docs/local_agent_kit/tools/kit.py` on the host.
      docker compose exec backend md5sum /tmp/<dir>/tools/kit.py
      ```

      `docs/` is not mounted into the backend
      container, so a container run silently tests the stale synced snapshot and reports
      green (see §0 Bucket 5f). A run that reports green without saying which files it
      tested does not count as a run. A phase that reports "I could not prove this" is more
      useful than one that reports an unproven green. (Team lead's ruling: briefing every
      agent is necessary but not sufficient, because a briefing is a hope and this failure
      mode looks exactly like success.)
