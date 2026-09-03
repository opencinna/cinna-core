# Local Agent Kit contract — cinna-core's answers to the Cinna Desktop handover

**What this is.** Cinna Desktop asked cinna-core for a versioned contract split of the
Local Agent Kit (`docs/agents/local_agents/cinna_core_handover.md`, revision 2). We built
it. This document is the reply: what we decided, what we found while building it, what it
costs you that we did not build, and what you have to change on your side.

**How to read it.** Sections 2 to 9 are the findings, ranked — the first is the one that
changes your plan most. Section 10 is the scope exclusion and its price. Section 11 is
the §8.2 exchange, built, with three disclosures you must act on. Section 12 answers your assumption table. Section 15 gathers every
required change on your side into one checklist; the sections above explain them, the
checklist is what you work from.

**Contract version shipped: `1.0.0`**, in three places inside one archive —
`CONTRACT_VERSION`, `kit.json` → `contract_version`, and `layout.json` →
`contract_version`. The serving path verifies the three agree and refuses to serve the
contract if they do not (section 14).

**A note on token spellings.** Every file in `docs/local_agent_kit/` — this one included —
is a *rendered* kit member: the platform substitutes placeholder tokens across the whole
tree before delivery. A document that needs to *discuss* a token therefore has to name it
bare, never in its braced form, or the server eats the example. So every token below is
written as `NAME`, `SLUG`, `KIT_VERSION` and so on; in a template file each appears
wrapped in double braces. That constraint is itself something you will meet the first time
you document a token in a file we render — see section 5, where it is also the reason a
defect hid.

---

## 1. Your three open questions

### Q1 — Where does the contract live inside an installed full `.cinna-kit/`?

**Merged at `.cinna-kit/` root. There will never be a `contract/` subpath.** Your
`contractStore` detector (`kit.json` + `layout.json` at the tree root) finds it with no
change: a full kit install already puts `kit.json`, `schema/` and `templates/` at
`.cinna-kit/` root, and `layout.json` and `CONTRACT_VERSION` now join them there.

The `VERSION` filename collision you flagged is resolved by not overloading the name:

- `.cinna-kit/VERSION` keeps its current meaning — the **kit content** version.
- The **contract** version is `kit.json` → `contract_version`, plus a literal
  `CONTRACT_VERSION` file at the same root.
- **The contract tarball ships no `VERSION` file at all.** Your `readVersionAt`
  (`src/main/kit/contractStore.ts:131`) falls back to `VERSION_FILE`, which is `'VERSION'`
  (`:38`). Change that fallback to `CONTRACT_VERSION`. `kit.json` remains the primary read
  and is always present, so the fallback should never fire — but as written it can only
  ever read the wrong number or nothing.

A consequence of the tarball shipping no `VERSION`: `kit.py`'s `_locate_extracted_kit`,
which requires `kit.json` **and** `VERSION`, can never locate a contract tree. That is
deliberate — a contract tarball is not a kit and must not be swapped in as one.

### Q2 — How much of `app-data/desktop.json` should the contract freeze?

**Your preference is right: exactly two keys, plus one optional third.** The file stays
yours. `layout.json` declares:

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

Either required key missing, empty, or the file absent means "not connected". `chat_path`
is optional and defaults to `/chat`, so the wire path is not frozen before you have built
the API.

Two things follow, and both are on the checklist:

- **The key names are `api_base_url` and `agent_token`.** You currently write
  `localApiBaseUrl` and `agentToken` — declared at
  `src/main/services/localAgents/desktopStateService.ts:45,47` and read by `coerce` at
  `:112-113`. We implemented the contract keys strictly and deliberately did **not** add
  camelCase tolerance, so `kit.py chat` against a folder the current desktop wrote will
  say "not connected". That is intended: a contract whose keys are optional is a
  convention with extra steps.
- **This block ships in the object form, which your parser drops.** See section 3.

### Q3 — Can the account API create and update an agent with no `Cloud/<host>/` workspace on disk?

**(a) Yes — but the answer splits, and the split is the important half.** Verified by
reading the account-CLI create/update path end to end, not inferred from the route list.

- Every DB-object action is purely server-side and needs no directory.
  `AccountAgentCreateBody` carries `name`, `description` and `user_workspace_id` — nothing
  path-shaped. `user_workspace_id` names a `UserWorkspace` **database row**, not a folder;
  the code says so explicitly (`backend/app/api/routes/cli.py:668-673`,
  `backend/app/services/cli/account_cli_service.py:654-658`): the active-workspace
  selection lives client-side in `.cinna/account.json`, and no server-side "active
  workspace" state is kept. The string `Cloud/` appears nowhere in `backend/app/**/*.py`
  outside the shipped copy of the kit docs.
- The **file tree** half needs *a* local directory, because the tree moves over Mutagen
  live sync and Mutagen syncs a directory. It does not need a `Cloud/<host>/`-shaped one.
  `Cloud/<host>/` is a cinna-cli and kit convention the server neither receives nor stores.

**And the caveat behind that "but" is the biggest thing in this document — it is section 2.**

**(b) `cinna agent import --update` resolving an entry by `platform_url` alone, with
`workspace` absent, is a cinna-cli change.** cinna-cli is a separate repository and out of
scope for this work; the change is recorded there as a follow-up. One correction that
matters for your implementation regardless: the entry does **not** live in
`cinna-agent.json`. It lives in a sibling `publications.json` at the agent root. See
section 6.

---

## 2. There is no tree-upload route. Your Publish design rests on one that does not exist.

**Rank: first.** This is a bigger obstacle than the §8.4 scope exclusion you were
expecting, and it is not a scoping decision — it is a fact about the server.

**What does not exist.** There is no account-CLI agent-update, prompts-write,
metadata-write, tree-upload, push or manifest-stamp endpoint. This is not an inference from
a route list; it was established by enumerating every route handler in
`backend/app/api/routes/cli.py` and reading the account-CLI create and update paths end to
end.

**What exists instead, and is usable:**

| Your Publish step | What is actually there |
|---|---|
| Create the agent | `POST /api/v1/cli/account/agents` (`account_create_agent`, `cli.py:859`) — stateless, no directory |
| Credential drafts, schedules, status, status-refresh command | Stateless account-CLI endpoints, no directory |
| Write prompts and metadata | `PUT agents/{id}` through the generic `account_api_proxy` (`cli.py:1734`) — which is exactly what `guides/11-go-cloud.md` already prescribes |
| Copy and push the exported tree | Mutagen live sync: `sync_stream_ws` (`cli.py:558`) → `CLIService.run_sync_tunnel` → env-core `/sync/exec`. Requires **a** local directory |
| Stamp the manifest | A client-side write into the local `cinna-agent.json` — not a server call at all |

**What that means for you.** Handover §8.3 speaks of "changes to the existing agent-import
path". There is no such path on the server to change. A desktop Publish flow therefore
needs either a local directory it can run a sync against, or a new server-side tree-ingest
route that does not exist today.

**cinna-core is not designing or building that route as part of this work,** and this
section deliberately offers no shape for one. We are telling you the gap is there so you do
not discover it after building against it.

**This also explains a fact on your side that would otherwise look like an oversight.**
`src/main/kit/exportTree.ts` is a well-built, tested view over a folder — file list, hash,
byte count — with **zero production callers**; its only importer anywhere in your tree is
its own test file. It has nothing to call it because the route it would push to does not
exist on ours. That is one obstacle, not two.

---

## 3. Your layout parser silently drops the `desktop_owned` block we ship

**Rank: second.**

**The mechanism, exactly.** `src/main/kit/layout.ts:228` reads the block as
`asStringArray(doc.desktop_owned).map(normalizeRelPath)`, and `asStringArray` (`:116`) is a
`typeof v === 'string'` filter. An array of **objects** therefore filters down to `[]`. No
throw, no `logger.warn`, no degraded-contract path: `KitLayout.desktop_owned` is typed
`string[]` (`:50`) and `desktopOwned()` (`:349`) simply returns nothing.

**The good news, stated as such: your own suite catches this.**
`src/main/kit/layout.test.ts:23` asserts `['app-data/desktop.json']` and will fail the
moment you pull the real contract file. This does not rot silently on your side.

**Blast radius today is small,** which makes this the cheapest moment the fix will ever be:
nothing consumes `desktopOwned()` behaviourally yet — the only other reference to the
concept is a comment in `desktopStateService.ts:3`.

**Why we shipped a shape we knew would break you.** The alternative was to ship your string
array alongside a parallel `desktop_owned_files` object key. That is two lists of the same
paths drifting apart forever, to spare an unreleased reader a one-line change. We chose the
shape that breaks **loudly and now** over the one that would degrade **quietly and later**.
The string form cannot carry `contract_keys`, which is the entire point of freezing the two
keys in Q2.

**This and the `desktop.json` key names are one position, not two demands.** We made the
identical trade in both places, for the identical reason, and in both places we rejected a
tolerance that would have let two shapes live forever. `parseLayout` should accept an entry
that is either a string or an object carrying a `path`, normalising both to one internal
shape. `kit.py` already accepts both forms, so either host can ship first.

**One related change worth taking in the same pass.** `desktopStatePath` in
`desktopStateService.ts` joins the agent directory to the constant
`DESKTOP_STATE_FILE = 'app-data/desktop.json'` (`src/shared/kit/manifest.ts:150`).
`layout.desktopOwned()` exists and has no behavioural reader. Reading the path out of the
contract instead of the constant beside it is the same move as our own
`desktop_state_file()` — and it is what makes parsing the object form worth anything.

---

## 4. We dropped the closed `enum` on `credentials[].type`. Your authored schema still has it.

**Rank: third.** A real divergence from the file you shipped, but a smaller obstacle than
either of the two above.

**What changed and in which direction.** The shipped schema's `credentials[].type` was a
closed 12-value `enum` — a hard error. It is now `"type": "string"` with an `examples`
array carrying the twelve current values and a description stating that these are the
platform credential types known at contract 1.0.0, that the platform's list grows
independently of any bundled contract, and that an unrecognised value is tolerated and
reported as a warning.

**We changed our schema to match your validator, not the reverse.** You reached
warning-level yourselves (`src/main/kit/validator.ts:353-359`,
`manifest.credentials.type_unknown`, "It will be sent to the platform as-is") and you were
right. It propagated from your validator into the contract artefact itself.

**The reasoning, because it will recur.** A closed enum inside a contract that is pinned,
bundled and carried offline is a time bomb with a date on it: the first credential type the
platform adds retroactively invalidates every folder that uses it, on a desktop that cannot
be updated to learn about it. The typo argument for keeping it closed does not survive
contact — a warning catches a typo just as visibly, and a genuinely wrong type fails at
import, where the authoritative list actually lives.

**Accepted cost, stated plainly: the schema no longer mechanically rejects an unknown
type.** That is a real loss. We took it because a contract's job is to describe a folder
shape that travels, not to mirror a server-side enum it cannot track.

`kit.py validate` now reports an unknown type as a warning too, so schema, `kit.py` and
your validator all agree. That is the consistent position rather than a two-against-one
compromise between validators.

**Your `resources/cinna-kit-contract/schema/cinna-agent.schema.json` still carries the
closed enum,** so this is on the checklist. It is one of two deliberate divergences from
your authored schema; the other is in section 6.2 and is the more urgent of the pair — see
section 9 for why they do not deserve equal billing.

---

## 5. We told you `KIT_VERSION` was a scaffold token. It is not one your scaffolder can fill.

**This one is ours, and it caused a bug in your code. Read our half first.**

**We published a token list that classifies `KIT_VERSION` as a scaffold token, with no
provenance caveat. Your `MANIFEST_TOKENS` enumerates it because our contract documentation
told you to — your enumeration is the correct implementation of the list we gave you.**

**What we failed to publish.** `templates/agent/cinna-agent.json` carries a `kit_version`
field whose value is the `KIT_VERSION` token, and `LocalAgentKitService` substitutes that
token **across every rendered member before delivery** (`_VERSION_TOKEN`). So in any kit or
contract tarball a client actually downloads, that token is **already resolved to a value**.
Six of the seven tokens are filled by the scaffolder on the user's machine; the seventh is
filled by the server before anyone sees it. Nothing anywhere said so.

**The consequence in your code.** `MANIFEST_TOKENS` (`src/shared/kit/manifest.ts`)
enumerates seven — `SLUG`, `NAME`, `DESCRIPTION`, `ID`, `CONTRACT_VERSION`, `KIT_VERSION`,
`CREATED_AT` — and your scaffolder will find nothing to substitute for the last of them.

**Why we are sending it now rather than after fixing our documentation.** It is latent on
your side, not firing: `grep -rn MANIFEST_TOKENS src/` returns its own declaration and the
derived type alias and nothing else. It is a bug waiting in your scaffolder, and it is
cheap to fix before it has a caller. Our own `cmd_new` substitution finds nothing there
either, harmlessly, because the value comes out correct either way — which is exactly why
neither host noticed.

**The rule that settles it, since a list cannot classify a member it contains:**
`KIT_VERSION` **is** a member of the scaffold-token set on purpose, and membership is not
the question. What settles it is **provenance**: it is the one member the scaffolder does
not fill. Our `kit.py` says this at the declaration site and has always said it; the
documentation we published had drifted away from a source comment that was accurate. The
caveat is now in the kit's `README.md` Placeholders section.

**The constraint that made this hide, which you will meet too.** A rendered member cannot
contain a token in its literal braced form — the server substitutes it before delivery — so
any document that discusses a token has to name it bare. That constraint quietly forced our
Placeholders paragraph into an awkward shape that *described* its tokens instead of writing
them, **and the awkwardness is what made the wrong list easy to miss.** A constraint that
deforms prose degrades the reviewability of that prose, and the defect then hides in the
deformation. Take the rule, and take that consequence with it: a team told only the rule
writes the same awkward paragraph and inherits the same blind spot.

**Separately, one severity fix in the same file that costs you one line.**
`checkContractCompatibility` (`src/shared/kit/contractVersion.ts:92`) returns `unknown` when
*either* side is unparseable, and `validator.ts:204-205` routes both cases to the same error
code, `manifest.contract_version.invalid`. So when your *app's own bundled* contract version
is unreadable, **the user is told their folder is broken** — exactly inverted from the
truth, and it points every resulting support conversation at the wrong artefact. `kit.py`
splits the two: an unparseable folder version is an error, an unparseable tool version is an
info saying the gate did not run.

---

## 6. The publication ledger moves out of the manifest

`publications[]` does not live in `cinna-agent.json`. It lives in a sibling
**`publications.json`** at the agent root.

**The defect that forced it, because it is worth understanding rather than accepting.** Both
hosts hash `cinna-agent.json` — neither exclude list contains it, and your own
`exportTree.test.ts:70` pins it as the first entry of the expected file list. So
`publications[].content_hash` would have been a value stored **inside the file it is a hash
of**. Reaching that fixed point requires producing a SHA-256 preimage on demand: not
difficult, impossible. Concretely: the first publish computes h₀ and writes it in, the
manifest bytes change, the next scan computes h₁ ≠ h₀, and the folder reads *"1 unpublished
change"* the instant the publish **succeeds**. Republishing to clear it creates the next
mismatch; publishing to instance B makes instance A read "behind". It is §9.3's "unpublished
changes forever" reached by a third route, and it is unfalsifiable from the user's side —
a number that never reaches zero with nothing on screen explaining why.

**Nobody decided this.** It is not a design anyone argued for; it is simply what you get by
writing the field into the file you already hash. We are reporting a defect, not assigning a
fault.

**Why not keep the keys in the manifest and declare a strip-rule instead** (hash a
canonicalised copy with those keys removed). That option is coherent and we would have taken
it if its fail-safe held. It does not, and the reason is in your parser: `parseLayout`
(`layout.ts:183-231`) builds its result field-by-field from a known key list and **silently
ignores any top-level block it has never heard of**, and your version gate passes any
same-major pair — pinned by your own test, `checkContractCompatibility('1.0.0', '1.4.2')` is
`ok` (`src/main/kit/contractVersion.test.ts:52`). So a strip rule shipped at 1.1.0 reaches an
existing desktop as **silence**: rule ignored, no warning, hash computed the old way, folder
reported healthy, number never zero. The only channel that reaches a non-adopting reader
loudly is a **major** bump, on a contract shipping at 1.0.0. It would also contract a
permanent cross-language canonical-serialisation obligation — Python and JavaScript already
disagree on `1e-07` versus `1e-7` and on integers past 2⁵³.

**The ledger file's shape.** Top level is an object, not a bare array: `{"publications": [...]}`.
A bare array has nowhere to put a format version or a sibling block, so the first such need
would be a breaking change for every reader. Its schema is
`schema/publications.schema.json`, shipped in the contract tarball and pointed at from
`kit.json` → `publications_schema`.

**Export never rewrites the manifest.** `kit.py export` copies `cinna-agent.json`
byte-identically; a legacy `cloud` block migrates at *write* time, never at export time.
Keep `publications?` on your manifest type as **tolerated and ignored** rather than dropping
it: a folder written before the split still carries it, and it reaches you until something
re-stamps it.

**One honest gap, so you do not go looking for a remedy that is not there.** There is no
re-stamp a user can perform today. `write_manifest` is the sole writer of `cinna-agent.json`
and its only caller is the scaffold path; none of `kit.py`'s verbs rewrites an existing
manifest. The first real caller will be a publish path or a re-stamp verb, neither of which
exists on any host yet. Until one does, a legacy folder keeps its `cloud` block and exports
it untouched — harmless, because it carries no secret and the platform ignores it.

### 6.1 Point the publication checks at `publications.json` — and move all of them together

**Fixing only `checkPublications` is worse than useless.** After the split,
`manifest.publications` is **always `undefined`**, because the sole manifest writer strips
the key on every write. So every read site takes its absent branch at once and none of them
says anything: `checkPublications` hits `if (publications === undefined) return`
(`validator.ts:513`) and emits no finding at any severity, and `scannerService` falls back to
`[]`. **Net effect: the desktop reports every published agent as never published, silently
— there is no error channel at all, on either the validator or the scanner path.** Fix the
validator alone and it goes quiet while the card keeps rendering "never published", which
removes the only signal the user would have had. The read sites move together or not at all.

**The checks themselves are right and they keep their severity.** `kit.py` implements the
same four conditions at the same error severity against the new file. Nothing about your
validation logic needs rethinking — the file it opens does.

The read sites: `validator.ts:512-540`, `scannerService.ts:420`,
`src/shared/localAgents.ts:249`, `ReadOnlyCards.tsx:233-246`, and the type at
`src/shared/kit/manifest.ts:122` (with `AgentPublication` at `:77`).

**Also fix the schema's `publications[].content_hash` description.** Your authored file
still carries the pre-secret-rule wording ("`cloud_import_excludes` applied"). A desktop
implementing `content_hash` from that description alone hashes a set that includes files
ours withholds, so the two hashes **never agree, silently, with no error on either side**.
Ours reads: over the files that survive both `cloud_import_excludes` **and** the
`secret_files` rules in `layout.json`.

### 6.2 `publications.json` must join your exclude list, or the *first* publish mismatches

This is nominally covered by taking our `layout.json` wholesale — the entry arrives with it
— but the consequence is not stated there and it is the sharpest one in that bullet.

`publications.json` is not in your `cloud_import_excludes` today (verified by loading
`resources/cinna-kit-contract/layout.json`). Your `collectExportFiles`
(`exportTree.ts:50-76`) keeps every path `layout.isExcludedFromExport` does not reject, and
`hashExportFiles` hashes exactly that list — so the ledger lands in **your hashed set** while
our exclude list withholds it from ours.

**The trigger is the first publish, not an exotic edge case.** The file does not exist until
something publishes and it exists immediately afterwards, on every folder. So this is not a
hash divergence you reach only if you happen to keep an oddly-named dotenv: it is the
default state of every published folder, on both hosts, from the moment publishing starts
working.

Two riders. It goes in as a **plain string, root-anchored** — never an object entry, which
`asStringArray` drops without a sound, and never in a `**/`-prefixed form, which would
withhold a nested `files/publications.json` that is an ordinary user data file and must
travel. (Verified on our side: root ledger excluded, `files/publications.json` still
copied.) And this is **not** a second ask on top of the re-bundle; it is the re-bundle's
consequence.

### 6.3 Our template made your scaffold's own promise unkeepable

**Your code promises this, in its own words** — `buildManifest`,
`src/main/services/localAgents/scaffoldService.ts:148-149`:

> Build the manifest from the template document, so unknown template keys and key order
> survive exactly as `kit.py new` leaves them.

**And our template is what broke that promise.** Your scaffolder parses the bundled template
document and sets seven fields on it; `copyTree` skips the manifest so it is written only
through that path. It faithfully preserves the template it is handed. The template it is
handed — `resources/cinna-kit-contract/templates/agent/cinna-agent.json` — still carries
`"publications": []` as its last key (verified now), while our `cmd_new` routes through
`write_manifest`, which strips the key on every write. **Preserving the template exactly is
precisely what makes your output diverge from ours.** The promise is unkeepable while the
template contradicts the writer.

**The fault is ours.** A template that contradicts the schema is a cinna-core defect, and
it is the artefact **we** ship in the contract tarball.

**The consequence you would have met.** A desktop-scaffolded folder trips our
manifest-still-carries-`publications` warning immediately — on a folder created seconds
earlier by the other host implementing the same contract.

**State of the two halves now, re-derived at this point of use.** Our template's key list is
`contract_version, id, kit_version, created_at, name, slug, description, example_prompts,
router_trigger_prompt, prompts, runtime, status_refresh_command, credentials, schedules,
handovers, features` — neither `cloud` nor `publications` present, and `cmd_new` now raises
if either reappears in the kit's own template. Yours is the same list **plus `publications`
at the end**. That is what leaves the divergence live.

**This is not a new ask.** The remedy is the wholesale re-bundle already on the checklist:
take our `templates/` with the corrected template and your scaffold's promise becomes
keepable again. Do not add it to your list a second time.

---

## 7. A `**/`-prefixed directory pattern cannot match at the root

**The mechanism.** In your `matchesPattern` (`src/main/kit/layout.ts`), the directory branch
only tries path prefixes at least as long as the pattern — so a `**/`-prefixed directory
pattern can never match at the root. `**/.mypy_cache/` does not match `.mypy_cache/x.json`;
`**/.git/` does not match `.git/HEAD`. Confirmed against your source, not against our port.

**Why this is worth more to you than the four patterns we are asking you to add.** Your
authored list pairs almost every entry — `.git/` with `**/.git/`, `.gitignore` with
`**/.gitignore`, `.gitkeep`, `credentials.json` — which suggests you discovered this
empirically and never wrote it down. **`__pycache__` is the one entry that only ever got the
`**/` form**, so a root-level `__pycache__/` has been travelling under contract semantics
(masked for `.pyc` files by `**/*.pyc`, but anything else in it went up). Every unpaired
directory entry in that list is a hole, and nothing tells you.

**What we did on our side.** We shipped every exclude-list addition in **paired form**, and
added `__pycache__/` alongside the pre-existing `**/__pycache__/` — one line fixing a
pre-existing hole in the same class. `layout.json`'s `cloud_import_excludes_notes` now names
the pairing requirement and why, so the apparent duplicates are not "simplified" away later.

Take the additions in paired form too, not the `**/`-only form.

**A related defect that was present identically on all three hosts, so none of us got a
mismatch alarm.** `matches_pattern` selects its directory branch from the **raw** pattern
(`pattern.rstrip().endswith("/")` on our side, `pattern.trimEnd().endsWith('/')` at
`layout.ts:275` on yours) while path normalisation strips only slashes. Trailing whitespace
therefore survives into the pattern **body** as a segment of its own, and the pattern then
matches nothing: `app-data/ ` matches neither `app-data/x.json` nor `app-data` nor
`app-data/sub/y`. No shipped pattern has stray whitespace and we now assert that in a test,
but a hand-edited contract would silently drop a directory from the exclude set and move the
`content_hash`, with every individual step still looking like it worked. **A faithful port
inherits the original's defects faithfully** — which is the property the port was written
for, so this is not a criticism of it.

**Update: two of the three hosts have now fixed it, and you are the third.** When this
section was first written our half was deliberately unfixed, on the reasoning that a
one-sided fix turns a shared blind spot into a cross-host divergence. That reasoning held
right up until cinna-cli fixed its half, at which point leaving ours alone *was* the
divergence, and we followed. Both of us now do the same thing:

- the pattern is **stripped** before the branch is chosen, so the branch test and the
  pattern body derive from the same text;
- and the strip is **announced** — a warning naming the pattern, so an author learns their
  pattern was read differently from how they wrote it.

**Implement strip-and-warn rather than choosing your own resolution.** Silent acceptance is
the one behaviour none of us should have: all three hosts hash the file set these patterns
select, and a difference of a single file makes the hashes disagree forever while every
individual step still looks like it worked. Rejecting a whitespace-bearing pattern outright
would also be defensible in isolation, but it would put you out of step with the two hosts
that have already moved, which is the thing this whole section exists to prevent.

---

## 8. Two token-egress properties to check in your own HTTP stack

These are properties of HTTP clients, not descriptions of what `kit.py` does. You are about
to build the other end of this contract in a different language with its own HTTP defaults,
and "your client may forward the `Authorization` header across a redirect" is worth more to
you than any clause we could add to the contract.

**Both were proven by a reverted-copy differential, not reasoned about.** With the guard
removed from a scratch copy, the foreign host and the recording proxy **actually received
the bearer token**. Both are now committed as tests.

### A redirect can carry the bearer token to another host

**With the guard reverted, the run exited 0 and printed the foreign host's answer.** That is
the sentence to act on: the leak's user-visible signature is **a chat that worked**, and
nobody investigates a successful chat.

The mechanism. Python's `urllib` copies the request headers onto the redirected request and
drops only `Content-Length` and `Content-Type` — `Authorization` survives — and for a POST
it auto-follows 301/302/303 as a GET. Node's `fetch`/`undici`, Electron's `net`, `axios` and
`got` each have their own answer to this and **none of them should be assumed**. There is no
redirect anywhere in the chat contract, so the safe policy on both sides is not "strip the
header and follow" but **do not follow at all**, and report the 3xx as the non-2xx it is.

### A configured proxy can carry it too

A default client honours `http_proxy` / `https_proxy` / `all_proxy` from the environment,
and `no_proxy` does not reliably cover loopback. On a developer machine with a corporate
proxy configured, the agent's bearer token and the user's prompt travel through it **on
their way to `127.0.0.1`**. A loopback API has no business behind a proxy: disable proxying
explicitly for this call rather than relying on loopback being exempt.

### And an error string is the cheapest way to leak a token or a URL

Several exception types on this path stringify an attribute of their own that holds the
request URL — and under the Q2 answer, `api_base_url` is file *contents* a tool never
prints. Format structured fields only: a status line, or the underlying socket error, never
the exception whole. The same trap exists in every language's exception types.

**A reader who implements "never print the token" alone will print the URL in the next error
line and believe the rule is kept.** Our assertions cover three things: the token is absent
from combined stdout and stderr, every other value from the state file is absent, **and the
`api_base_url` itself is absent**.

---

## 9. The failure shape you will meet most often: a filter used where an assertion belongs

Named as a shape rather than as a list of unrelated findings, because you are about to write
a great deal of code against this contract.

**A filter is used where an assertion belongs, so a missing or wrong case degrades silently
to a plausible-looking success instead of failing loudly.** Found repeatedly, independently,
on both sides:

- `asStringArray` (`layout.ts:228`) filters the `desktop_owned` object form down to `[]`
  instead of rejecting it — no throw, no warning, no degraded-contract path (section 3).
- Our contract-tarball packer selected members with a *filter* rather than an assertion, so
  an incomplete contract — missing `layout.json` and `CONTRACT_VERSION` — shipped as a 200
  with a perfectly valid ETag (section 14).
- A test run in our backend container resolved to a stale snapshot instead of the working
  tree and reported green having exercised none of it.
- `kit.py validate` exited **0** on an agent whose `.claude/` was `chmod 000`.
  `_validate_secrets` walks the whole folder hunting stray key material with `os.walk`, which
  **yields nothing for a directory it cannot enter** — no error, no marker. The one check
  whose entire job is "there is no credential material in this tree" reported a clean bill of
  health over a subtree it never saw. A crash would have been the cheaper outcome.

**One instance did something worse than lose information — it invented it.** For an
unreadable *file* during validate, the obvious repair was to catch `OSError` in the two read
helpers and return `""`. That would have reported an unreadable workflow prompt as an
**empty** one: not a lost finding but a **wrong** one, moving the reader from neutral to
wrong, and strictly worse than the bare errno it replaced. The shape you will meet is not
only "a filter swallowed a failure" but "a filter turned a failure into a different,
plausible finding".

**And one instance of it was in the guarantee we asked you to adopt.** Our
`is_secret_filename` treats an unreadable rule as "assume it protects something" and
withholds everything, and its docstring promises exactly that. One function upstream,
`secret_file_rules()` filtered non-dict rules out of the contract list before they could ever
reach it. **The consumer's fail-safe was unreachable and the docstring's claim was false** —
on the one list whose failure mode is a credential leaving the machine. It is fixed by
removing the filter. The secret-rule ask still stands and is still right; the record has to
carry this alongside it.

**The transferable half of that, and it is a specific instruction rather than an
apology.** Our own mutation table would have passed. It mutated rule *contents* —
`basename_equals: [123]`, an unknown clause key, an empty `match`, a `match` that is a list —
every one of which survives an `isinstance(rule, dict)` filter and reaches the consumer. The
defect was in rule **shape**, which does not survive it. **A correct test of the wrong
layer.** So when you adopt the secret rule, test your adoption **at the shape layer**: feed
your rule reader a rules array whose *entries* are the wrong kind of thing, and assert the
gate still withholds. A team that reads the honest version of this tests it that way; a team
that reads a flattering version tests it the way we did.

**A related heuristic, and what it does and does not buy — worth saying honestly because you
are about to adopt the same kind of thing.** The heuristic is: *two adjacent statements doing
the same job, one guarded and one not, is itself the finding* — either the new guard is
unnecessary or the old line is missing one, and both cannot be right. We wrote it into this
project's review checklist in direct response to a defect of exactly that shape. Hours later,
new code written by someone who had been briefed on it **reproduced exactly that defect** —
an unguarded key removal beside a type-guarded one doing the same job.

- **The checklist entry did not prevent the defect. It made the author able to find it — in
  their own first cut, before review.** Budget for it as detection, not prevention, or you
  get surprised twice: once when the bug happens anyway, and again when you conclude from
  that the heuristic is worthless.
- **That it recurred so fast says the shape is not a lapse in attention.** It is what the
  work naturally produces when a guarded line and an unguarded line do the same job in one
  function: the guard is written correctly for the case in front of you, and the neighbour is
  not in front of you. Treating it as carelessness leads to asking people to be more careful,
  which is the one remedy that has now failed twice here. Treating it as a shape leads to a
  check applied to the code *surrounding* a change — which is what caught it.

One rider, because a document that only records a heuristic's hits gets it applied as though
it never misses: it has a real false-positive rate. It fired on a guarded newline write
sitting beside an unguarded flush, where the asymmetry was correct and deliberate. The right
response there was a comment, not a change.

**Three more things that generalise, briefly:**

- **A wrong explanation is worse than a missing one.** An unexplained thing leaves a reader
  uncertain, and uncertainty is what sends someone to go and check. A falsely explained one
  stops them looking and leaves them confidently wrong. We hit this twice: a report asserting
  a cross-reference comment that did not exist (then asserted again, independently, in the
  source), and a docstring justifying a correct fallback with a false reason — the code was
  right and the only casualty was the next reader's belief about what the contract contains,
  including a reader deciding what their own client must fetch.
- **A shared contract does not buy you regex parity.** `$` matches before a final newline in
  Python and does not in JavaScript. We shipped a matcher that diverged from yours on
  filenames ending in a newline — legal on macOS and Linux — which is a different file list
  and therefore a different `content_hash`. No amount of shared *data* prevents this, because
  it lives in the host regex engines. **The differential harness, not the contract, is what
  catches this class**, and it is worth your running one too.
- **The fail-safe direction is a property of the consequence, not a house style.** For secret
  rules, unevaluable ⇒ treat as secret and withhold the file. For anything affecting a hash,
  unevaluable ⇒ refuse to emit `content_hash` at all, never emit one computed a different
  way. The two run in **opposite** directions because the consequences do: a leaked secret is
  unrecoverable, while a plausible wrong drift number is untraceable and a missing one is
  merely visible. A host that copies the direction instead of deriving it will get the next
  case backwards.

---

## 10. §8.3 and §8.4 are out of scope. Here is what that costs you.

**In scope and delivered:** handover §2 (contract/guides split, `layout.json`, contract
tarball and version endpoint), §3 (manifest schema), §4 (versioning rules and the
compatibility gate), §5 (`kit.py` changes including the new `chat` verb), §6 (templates and
guides), §7 (default workshop path, now `~/Documents/CinnaAgents`), §9 (the conformance
affordances). §8.1 and §8.2 are built — section 11.

**Out of scope, deliberately:** §8.3 server-side import changes, and §8.4 "account-CLI
endpoints accept desktop tokens". Also §10 in its entirety (cloud→desktop relay, proxy agent
type) — do not build toward it.

**What that costs you, stated plainly: with §8.2 but without §8.4, the silent link works and
Publish does not.** Your own degraded path from §8.4 — the agent page showing the export
summary and a "run `cinna agent import` from `Cloud/<host>/`" instruction — is the shipping
path for now.

**Everything you need in order to *detect* publication state still lands.** The ledger
(`publications[]` with `platform_url`, `agent_id`, `workspace`, `imported_at`, `updated_at`,
`contract_version`, `content_hash`) and the `content_hash` algorithm are both in the
contract. What changed is only the mechanism they ride on: the ledger is in
`publications.json` rather than in the manifest (section 6), and the hash is computed by
`kit.py`. Twin matching, drift, and per-instance status all still work through the CLI import
path.

**And read section 2 with this.** §8.3 is not merely deferred — it describes changes to an
import path that does not exist. That is a different fact from a scoping decision, and it
lands on the same feature.

---

## 11. §8.2 `POST /api/v1/cli/account/desktop-token` — built. The contract, and three things you must act on

The placeholder that stood here is gone. What follows is the endpoint as registered, then
three disclosures. **Read the disclosures even if you already coded against the shape** —
two of them change what your app must do after it receives a token, and one changes what
you may tell a user about revocation.

### The endpoint

**Path and method, as registered:** `POST /api/v1/cli/account/desktop-token` — the `/cli`
router's `/account/desktop-token` route, in `backend/app/api/routes/cli.py`. It lives on the
CLI router and not on `/desktop-auth` because of what authenticates it.

**Authentication:** the CLI **account** token, as a bearer credential, the usual account-CLI
way (`Authorization: Bearer <the token from account.json>`). The token is used, never
stored, and is neither consumed nor revoked by the exchange, so a desktop that lost its
refresh token can simply exchange again. Anything else — a per-agent CLI token, a web or
desktop JWT, no credential, a revoked or expired account token — is refused with **401**.

**Request body** (JSON; every field optional):

```json
{
  "client_id": "<your desktop client id, if you have one>",
  "device_name": "Evgeny's MacBook",
  "platform": "macos",
  "app_version": "1.2.3"
}
```

Send `client_id` when this desktop already has one for this instance. Omit it (or send
`null`) the first time, together with the three display fields, and a client is registered
for you through the identical lazy-registration path the browser consent uses. There is
deliberately no field for the client's origin: provenance is set by the server and never
accepted from a caller.

**Response body** (200) — the `/desktop-auth/token` shape plus `email`:

```json
{
  "access_token": "<JWT>",
  "refresh_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 900,
  "client_id": "<the client the pair is bound to>",
  "email": "<the account these tokens belong to>"
}
```

`expires_in` is `DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES × 60` (900 with the default of 15).
Store `client_id` — a lazily registered desktop learns its id here. Refresh the pair
afterwards through the ordinary `POST /api/v1/desktop-auth/token` with
`grant_type=refresh_token`; nothing about the session is special after issuance, so
rotation, replay detection and the reuse-grace window all apply to it unchanged. **`email` is
not a convenience field. Section "Disclosure 2" below says what you must do with it.**

**Error cases:**

| Status | When |
|---|---|
| 401 | The bearer credential is not a live account CLI token (missing, wrong type, revoked, expired) |
| 403 | `client_id` names a client that is revoked, unknown, or belongs to another user. The three are **deliberately indistinguishable** to the caller, so a client id cannot be probed for existence; our audit log tells them apart. Treat it as "this client id is no longer usable here": drop it and either exchange again without one or send the user through the browser consent |
| 422 | Body validation (a field over its length limit, wrong type) |
| 429 | More than the per-account-token ceiling of exchanges in a minute (`DESKTOP_TOKEN_EXCHANGE_LIMIT_PER_MIN`, 10). Every `client_id`-less exchange registers a client row, which is why the ceiling exists. Back off; do not retry in a loop |

**How the issued client appears in `GET /api/v1/desktop-auth/clients`:** as an ordinary
client row with `origin: "cli_exchange"` (a browser-consented one reads `browser_consent`).
Settings → Security → App Sessions renders that as a **CLI link** badge. `origin` describes
the client's *most recent grant*, not how the row was created: a browser consent on the same
`client_id` later flips it to `browser_consent` and retires the CLI-minted refresh family
(scoped to that one client — the user's other devices are untouched), and a CLI exchange onto
a browser-registered client flips it the other way. Both outcomes of every exchange are
audited (`CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED` / `_DENIED`), never with a token value.

**How it is revoked:** disconnecting it in App Sessions (`DELETE /desktop-auth/clients/{client_id}`),
or revoking the account CLI token that bought it (`DELETE /api/v1/cli/account/tokens/{id}`,
or the Settings card) — the latter cascades into every desktop session that token bought, and
the session is rejected on its **next request**, not merely at its next refresh. Then read
disclosure 1, because that sentence is not the end of the story.

**Two settled properties, restated so they are not re-derived from the shape:** the exchange
is **not role-gated**, deliberately — an agent-user can hold a linked session and the backend
refuses what they may not do (a publish, say) when they ask for it, which is the layer that
decision belongs to. And the exchange is **not bound to a machine**: an account token is a
bearer credential with no device binding, the machine name is self-reported, and the endpoint
checks no IP, origin or device. Do not describe the silent link to users as "only works on
this computer".

### Disclosure 1 — a linked session can mint a credential that outlives every revocation control we have

We reproduced this by execution against the shipped tree, with a positive control in the same
run. A desktop session obtained through this exchange is an ordinary user JWT. It is
**refused** when it tries to approve a desktop or mobile consent, mint a CLI setup token, or
approve a `cinna login` — those surfaces are gated, because each would mint a credential with
no link to the account token, and revoking the token would then end nothing. **It is not
refused on the platform's MCP OAuth consent.** Through `POST /mcp/consent/{nonce}/approve`
and `POST /mcp/oauth/token` it can mint an App MCP access + refresh pair bound to the user.
Then:

- revoking the account token kills the desktop session (401 on the next request, 400 on
  refresh) and reports "2 session(s) disconnected";
- the MCP refresh token keeps answering **200**, for up to thirty days from issue, through
  any number of refreshes;
- it appears in **no** list — not App Sessions, not the connector's token card (which lists
  direct tokens only) — so its owner cannot discover it;
- the one route that revokes it, `POST /mcp/oauth/revoke`, takes the token **value** as a
  form field and enforces no authentication. The thief holds exactly what it requires; the
  victim holds nothing it accepts.

Its only teardown today is deleting the user. We have **not** fixed this in this delivery —
wiring those tokens into the cascade and giving them a listing and an owner-reachable revoke
is another feature's territory and is being raised separately. What we have done is correct
our own documentation, which until now told a user that revoking the account token ended the
session and its children and hedged only with "not a complete remediation".

**What this means for you:** never tell a user that disconnecting a session or revoking the
account token ends everything that session did. If your app has any leak-response guidance,
it must say: revoke, then rotate the credentials that session could read, and — until the gap
is closed on our side — assume an MCP credential may still be live for up to thirty days.
Do not build a feature on the premise that App Sessions is a complete inventory of what a
linked session can hold.

### Disclosure 2 — a stranger's approval can hand your app someone else's session. You must verify the account.

Pre-existing, not caused by this delivery, and it inverts something we nearly sent you. A
consent request that names no `client_id` — the lazy-registration flow your first sign-in
uses — has no owner until someone consents. **Any authenticated holder of the nonce may
approve it.** Your app started the flow and holds the PKCE verifier, so your app is the one
that redeems the resulting code — and it is then authenticated as **the approver**, not as
the user who typed the instance URL. `/userinfo` returns the stranger's email. Everything the
user does from then on lands in the stranger's account. The reproduction is committed as
`test_token_response_email_reveals_a_substituted_account` in
`backend/tests/api/desktop_auth/test_desktop_auth.py`.

An earlier draft of this document argued that *deny* was the dangerous branch (anyone holding
the nonce can burn a pending request) while *approve* "mainly harms the misuser". The deny
half stands and is a residual we accept by decision — the nonce travels only in the
requesting browser's URL, and there is no owner column to bind a lazy request to, nor will
there be. **The approve half was wrong, and approve is the more severe branch.** Nothing in
this document or ours describes approve as self-limiting any more.

**What we changed:** every desktop token response — this exchange, `POST /desktop-auth/token`
on code exchange, and on every refresh — now carries `email`, the account the tokens actually
belong to. Additive and non-breaking. One builder owns the response shape, so the field cannot
reach some issuance paths and not others.

**What you must do, because the field makes the substitution visible and does not prevent
it:** compare `email` against the account your user expected — the profile they are signing
in to, or the one already stored for that instance — on the code-exchange response **and on
refresh responses**, and refuse the session (discard the tokens, tell the user) when it
differs. A client that ignores the field is exactly as exposed as before the field existed.
Do not treat its presence as the fix; the fix is your comparison.

Our consent page shows "Signed in as …" and offers "Use another account", which protects
against the *user's own* wrong-account case; it does nothing for this one, because the
stranger's browser is the one on the consent page.

### Disclosure 3 — the credential-minting gate on `/consent` refused *deny*, and now does not

The gate that stops a linked session from minting a fresh native session was, until this
round, a route-level dependency on `POST /desktop-auth/consent` and `POST /app-auth/consent`,
so it refused `action="deny"` as well as approve — the safety action, which mints nothing.
It now runs inside the handler on the approving branch only, keyed on "not deny" so an action
added later is gated by default. Observable behaviour for a CLI-linked session driving the
consent endpoints directly: `approve` → **403** with a message naming the reason ("linked from
a CLI account token … cannot grant new credentials or approve new sign-ins"); `deny` →
**200** with the normal `error=access_denied` redirect. Nothing changes for a browser session.
If your app ever presents its own token to a consent endpoint (we found no path where it
does), this is what it will see.

### What we could not verify: the mobile `/app-auth` surface

The mobile surface shares the service, the tables, and — as of this round — the in-handler
gate placement, and our tests exercise both surfaces for every behaviour above. **We did not
verify anything about the Cinna Mobile app itself**: its repository is not on this machine,
and nothing in this document should be read as a claim that the mobile client handles `email`,
the 403-on-approve, or the consent flow correctly. That is stated as unverifiable here, not
as checked.

---

## 12. Your assumptions, confirmed or corrected

| # | Verdict | What we found |
|---|---|---|
| **A1** | **Confirmed**, and one rider on our own brief was wrong | The guides did default to `~/Documents/MyAgents`, in `START.md`, `assistants/codex.md`, `guides/11-go-cloud.md` and `guides/05-schedules.md`. They now say `~/Documents/CinnaAgents`. The path was **not** confined to the kit: five more references existed outside it, including a user-facing frontend surface (the onboarding modal's "What it creates" folder tree). All swept. |
| **A2** | **Partly wrong** | The second half is right: `cmd_new` knew nothing of `DESCRIPTION`, `ID`, `CREATED_AT` or `CONTRACT_VERSION`. The first half is not: `substitute_tokens` substituted **lowercase** `name` and `slug` across the template tree, and the manifest template carried literal `"New Agent"` / `"new-agent"` rather than tokens. `KIT_VERSION` was never a `substitute_tokens` token at all — it is server-rendered (section 5). Adopting your UPPER_SNAKE set was therefore a rename across every template, which is done. |
| **A3** | **Confirmed, and incomplete** | The list did live in `kit.json` → `cloud_import.exclude`, read by `kit_config()` / `cloud_import_excludes()`. But there were **three** lists, not one — plus `DEFAULT_EXCLUDES` and `ALWAYS_EXCLUDE` (re-added unconditionally at export) — and a fourth filter that is not a list at all: a dotenv-filename short-circuit applied independently. The consolidation onto `layout.json` folded in all four. |
| **A4** | **Answered** | `Report` has four channels — `error` / `warn` / `info` / `fix` — and `fix` is used by exactly one check, the pyproject ⇄ `workspace_requirements.txt` reconciliation, which can indeed auto-repair. `report.ok` is `not self.errors`; warnings never fail a run. A `--cloud-ready` flag rebinds a *set* of checks from warn to error, which has no analogue on your side. The full severity map is section 13. |
| **A5** | **Confirmed** | `template_description()` reads the scaffold template's `description` and detects a manifest still carrying it; `_validate_cloud_readiness` gates the cloud-ready checks. You have no equivalent of the first, and **you do not need one** — it is on the kit-only list and is a warning, so §9.2 parity does not require it. |
| **A6** | **Confirmed, with a consequence the handover did not anticipate** | `cmd_list` and `_rungs_present` do read the `cloud` object for the cloud-state column. But `cmd_list` also **hard-coded the flat cloud workspace** — `root/Cloud/.cinna/account.json` and `root/Cloud/agents` — so the `Cloud/<host>/` move was a code change in `kit.py`, not a docs change. It is done, and the legacy flat layout still works and still lists. |
| **A7** | **Answered — `/contract.tar.gz` is consistent with our naming** | The kit tarball is served at `/agent-start/kit.tar.gz` (and `/api/agent-start/kit.tar.gz`), archive rooted at `cinna-kit/`, download filename `cinna-kit.tar.gz`. So `/contract.tar.gz` rooted at `cinna-contract/` with filename `cinna-contract.tar.gz` matches exactly. |
| **A8** | **Confirmed, plus a material correction** | Every §8 endpoint was unbuilt: `contract_version` and `publications` returned zero matches across `backend/` and `frontend/`; `cinna-agent.json` is never parsed, validated or stored server-side; the manifest `cloud` block was written by `kit.py` and never read back. §8.1 is now built. **The correction is section 2:** §8.3 describes changes to a server-side agent tree-import path that does not exist. |

**Two more from your table, since we found the answers anyway:**

- **A9** — you asked us to check rather than asserting anything, and **you were right to
  worry**. The export was leaking suffixed dotenvs: the exclude list *enumerates* dotenv
  suffixes, so `.env.production`, `.env.staging` and `.env.development` travelled at any
  depth **on both sides**. A separate defect was eating `.env.example` (because
  `".env.example".startswith(".env.")` is true). Both fixed, and the rule is now declared
  data in `layout.json` — see the checklist. `credentials.json` and key material were not
  at risk: `credentials/` is excluded wholesale and your authored list already covers
  `credentials.json` and `**/*.pem|key|p12`.
- **A10** — **confirmed.** `kit.py` had no YAML dependency; it scanned lines. On the
  severity question you asked: an unreadable command entry used to be **silently skipped**,
  which took the Makefile-mirror check, the duplicate check and the `/run:` resolution blind
  with it. It is now an **error**, matching your `commands.unparseable`.

---

## 13. Validator parity (§9.2)

**§9.2 is a manual comparison today, and will be until both sides carry finding codes.**
Plan around that. `kit.py`'s `Report` has four channels and each finding is a bare sentence;
your `Finding` carries `{code, message, path}`. So a harness keyed on `code → severity`
cannot be run against `kit.py` as it stands — severities can only be diffed by matching
message text. Adding codes to `kit.py` unilaterally buys nothing until you change too, and
the change is additive and non-breaking whenever it happens, so we deferred it rather than
doing it badly. **Your `{code, message, path}` shape is the sensible one and is what both
sides should converge on.** Note this is a shared gap, not a debt one side owes the other: a
`code → severity` harness will not round-trip on your side either — you emit
`manifest.features.type` at two different severities (error when `features` is not an object,
warning when a member value is not a boolean), and your folder-read failures carry a doubled
prefix (`manifest.manifest_invalid_json`, `manifest.manifest_not_found`) that your UI keys
on, so it is not to be "fixed".

### Severity changes we made to align with you

Every check `kit.py` has that your validator does not is now a **warning or info, never an
error** — a folder the kit calls broken that the desktop runs happily is the parity failure
in the worse direction. Demoted from error to warning: missing `.gitignore`; empty workflow
prompt; workflow prompt still holding a placeholder; `credentials/.env` tracked by git;
pyproject ⇄ `workspace_requirements.txt` reconciliation (the `--fix` repair is unchanged;
only the exit code moved). Kit-only and left as they were: expected-files warnings, the
key-material filename warning, "schedules but no `status` command", and the whole
`--cloud-ready` promotion mechanism, which is kit-only by construction.

On checks we both have, `kit.py` moved to your severity: duplicate credential slot name,
unknown credential `type`, and a handover target folder that does not exist next to the
agent are all now warnings. Command names reconciled: your
`^[A-Za-z0-9][A-Za-z0-9_-]*$` is now the **error** threshold and the kit's stricter
lowercase/≤32 rule became a **warning** ("convention"), so neither side is surprised.

**One deliberate divergence, and we are asking you to close it rather than closing it
ourselves.** A credential slot carrying a forbidden value key (`value`, `secret`, `token`,
`api_key`, `client_secret`, `private_key`, `credential_data`, …) stays an **error** in
`kit.py`. Demoting a secret-leak guard to match a validator that does not have it is the
wrong trade. **Please add the check.**

### Checks of yours we adopted

Added to `kit.py` at your severity: `commands.run_reference_unresolved` (error),
`commands.unparseable` (error), `example_prompts` bounds — more than 20, item longer than
500 (error), self-handover (warning), unroutable — no router trigger and no examples
(warning), status-file frontmatter and field checks (warning), non-boolean `features` member
(warning), `scripts/` with no `scripts/README.md` (warning), and deprecated `cloud` block
(info). `runtime.credential` that looks like a secret — `sk-`, `sk_`, `ghp_`, `gho_`,
`xox[baprs]-`, `AIza`, `AKIA`, or longer than 200 characters — is an **error** on both sides.

One of yours we did **not** port, as a decision rather than an oversight: `contract.older`
(your `checkFiles` step 7), an info fired whenever the folder's contract sorts below the
tool's on any component. It duplicates the migratable warning for the case that matters.

### Two inconsistencies inside your own pair, for your benefit

- Bounds your **validator enforces that your schema does not declare**: `checkIdentity` runs
  `contract_version` (`validator.ts:189`) and `id` (`:209`) through `checkString(..., 64, ...)`,
  yielding `…too_long` findings; the schema declares no `maxLength` on either. Inert, but
  undocumented.
- Bounds your **schema declares that your validator does not enforce**:
  `credentials[].description` and `handovers[].description` both carry `maxLength: 2000`;
  `validator.ts:387-393` and `:481-487` check only that the value is a string.

---

## 14. Other changes on our side you should know about

### `content_hash` (§9.3): three details the prose does not carry

We implemented §9.3 verbatim, with one correction and two things worth stating because
anyone reimplementing from the prose alone gets them wrong.

- **The unreadable marker emits two NULs, not one.** `exportTree.ts` sets it to
  `'\0unreadable'` and emits `` `${rel}\0${hash}\n` ``, so the line is
  `<relpath> NUL NUL "unreadable" LF`. §9.3's prose describes a single NUL. Proven by
  construction.
- **The UTF-16 sort key is load-bearing, not a portability nicety.** A tree containing a
  non-BMP filename yields *different digests* under Python's default `sorted()` versus a
  UTF-16 code-unit ordering; the UTF-16 key is the one that matches your output. We sort
  with `key=lambda p: p.encode("utf-16-be")` and say why in a comment.
- **`?` consumes one UTF-16 code unit, not one code point.** Your implementation was already
  correct and ours was not. Stated here so neither side "simplifies" it later: a non-BMP
  character is *two* code units, so `?.md` does not match a single-emoji filename and `??.md`
  does.

**Verification, so you know how far the parity claim reaches.** Your `matchesPattern`,
extracted read-only into a scratch harness during implementation, ran against a 6557-case
differential with **0 mismatches**, including trailing-newline names, nested newlines,
`?`/`??`/`???` patterns and non-BMP versus BMP paths. (That harness was scratch work and is
not in the tree; the figure is the one that run recorded.) `collectExportFiles` + `hashExportFiles` were run over synthetic
trees including symlinks, a `chmod 000` file and non-BMP names: file list identical,
`contentHash` byte-identical; an empty tree hashes to `sha256:e3b0c442…b855` on both sides.

**One divergence we cannot close by implementation, and it needs a contract decision from
both of us.** A non-UTF-8 filename decodes differently in the two runtimes — Python with
`surrogateescape`, Node with lossy U+FFFD replacement. Different strings, different digests,
whatever either side encodes with. Parity is unreachable for such a name. The fix is a
contract decision: agree a decoding, or exclude such paths. We have made neither choice
unilaterally.

**And one convergence rather than an ask:** our export now refuses to emit a `content_hash`
when any file in the set could not be read, which matches the behaviour your own
`ExportTree.unreadable` docstring demands.

### The contract endpoints, and how they fail

`GET /agent-start/contract.tar.gz` and `GET /agent-start/contract/version`, both also under
the `/api/agent-start` alias. Both are unauthenticated, both sit behind the same
`local_agent_kit_enabled` 404 guard and the same rate limiter as the existing kit surface,
and both carry per-representation ETags, `X-Kit-Version` and `Cache-Control`.

`/contract/version` returns the `/version` envelope plus the new key:

```json
{
  "kit_version": "…",
  "contract_version": "1.0.0",
  "platform_url": "…",
  "kit_base_url": "…",
  "start_url": "…",
  "instance_name": "…",
  "cli": { "install_spec": "…", "min_version": "…" }
}
```

**ETags are keyed on `kit_version`, not on `contract_version`, and that is deliberate.**
`contract_version` is hand-maintained and does not move when a template or the schema
changes, so an ETag keyed on it would answer "unchanged" to a client that is in fact holding
a stale contract.

**The archive has one top-level directory, `cinna-contract/`.** Your handover says the
tarball "extracts to a tree whose root holds `kit.json` and `layout.json`" — true of the
extracted directory, not of the archive's top level. You must descend one level, exactly as
`kit.py`'s `_locate_extracted_kit` already does for `cinna-kit/`.

**Both representations degrade together, and they fail loud.** We found that they did not:
`/contract/version` failed loud while `/contract.tar.gz` degraded **silently** — a 200 with
a truncated archive and a perfectly valid ETag — because the member selector was a filter
rather than an assertion (section 9). On the instance we were developing against,
`/contract/version` returned 503 while `/contract.tar.gz` returned 200 with a 33-member
archive missing both `layout.json` and `CONTRACT_VERSION`. A desktop pulling that gets a tree
its own `contractStore.isContractTree` will not recognise, with nothing anywhere explaining
why.

Now: both 503 together whenever `kit.json`, `layout.json` or `CONTRACT_VERSION` is absent or
unparseable, **or when the three declared `1.0.0`s disagree**. A thin `schema/` or
`templates/` is a *content* problem and still serves 200 on both — a content problem must not
be dressed up as an identity failure. And the rest of the anonymous kit surface — `START.md`,
`/version`, `kit.tar.gz`, `/kit/{path}` — keeps serving 200 regardless of contract health,
because that surface's whole design premise is that it degrades gracefully for a stranger.

### `schema_version` has left `GET /agent-start/version`

**This is a change to an endpoint, not only to a file, which is why we are telling you rather
than letting you notice.** `GET /agent-start/version` — unauthenticated, served by every
instance, the endpoint `kit.py refresh` polls — used to return a `schema_version` key on
every response. It no longer does. `docs/local_agent_kit/kit.json` carried the same field and
lost it in the same change; the two moved together, because a payload disagreeing with the
shipped file reads as a serving bug rather than a deliberate removal.

**This costs you nothing.** Your authored `kit.json` never carried the field — its top-level
keys are `name`, `title`, `description`, `contract_version`, `schema`, `layout`, `templates`,
`refresh` — so the removal moves toward your file. And every `schema_version` reference in
your tree concerns the **manifest**, never `kit.json`.

**The distinction, because confusing the two would be a serious regression.** The
**manifest's** `schema_version` — in `cinna-agent.json`, in the schema's legacy-exemption
`allOf`, and in `kit.py`'s identity check — is **real, live, load-bearing and stays**. It is
the sole selector for the one tolerated identity absence (`schema_version` present with
neither `contract_version` nor `id` ⇒ pre-1.0.0 folder, warned and re-stamped, never
rejected), your `validator.ts` and its tests pin it, and the CHANGELOG's Compatibility table
depends on it. Only `kit.json`'s went. The two share a name and nothing else.

**Why removing a field from a public endpoint was safe, since "no reader in either
repository" cannot answer that.** The value was a **synthesised constant**: the literal `1`
was compiled into the payload builder and no input could ever have made it vary. A
hypothetical external consumer reading it learns nothing from it, and nothing downstream can
compute anything from a value that has only ever had one value. **A constant field is exactly
the one whose removal breaks only code that reads it without using it** — that property, not
an empty grep, is what carries it, and it is the question to ask of the next field, which may
not have it.

### The Compatibility table is byte-identical to yours

`docs/local_agent_kit/CHANGELOG.md` now carries handover §4's Compatibility table verbatim —
diffed against your `resources/cinna-kit-contract/CHANGELOG.md` section and identical byte
for byte, heading and blank line included. Three implementations key off that table and the
schema's identity comment says "if you change one, change all three", so byte identity is the
property, not paraphrase.

### The contract ships a `.claude/` directory

`templates/agent/.claude/settings.local.json` is a contract member, so a scaffold
materialises a `.claude/` directory the desktop does not own. Naming it explicitly because
from your side it otherwise reads as a bug.

### The shipped template is not a valid manifest — on both sides, by construction

Any test either side writes asserting that the shipped template validates against the shipped
schema **will fail**. Ours ships unsubstituted scaffold tokens including a `slug` whose value
is the `SLUG` token; yours ships the same shape, and your `MANIFEST_TOKENS` guarantees it
stays that way. The schema's slug pattern (`^[a-z0-9][a-z0-9-]{1,62}$`) rejects it before any
identity branch is reached.

**The resolution, and the one option that must not be taken.** The test must validate a
**scaffolded** manifest, never the raw template. Loosening the schema patterns to admit token
strings would make the schema stop rejecting a folder whose scaffold never ran — precisely
the case it exists to catch. Better you read this from us than spend the afternoon we spent.

### `kit.py list` changed — not a contract surface, but if you parse it, it moved

A courtesy heads-up, not a required change. `list` output has never been a declared contract
surface, and this is filed as Changed rather than Breaking. Two things moved: the table gained
a fifth column (`SLUG`, `NAME`, `RUNGS`, `CLOUD`, `DESKTOP`), and the `CLOUD` cell now answers
`yes` for a folder carrying a `publications.json` ledger where it previously answered `no` —
the same question, asked of the file the ledger moved into. If you parse it positionally, it
moved.

---

## 15. Everything you must change on your side

One list, and every item above points here rather than asserting its own position in it.

- **Change your contract-version fallback from `VERSION` to `CONTRACT_VERSION`.**
  `readVersionAt` (`contractStore.ts:131`) falls back to `VERSION_FILE = 'VERSION'` (`:38`);
  the contract tarball ships no `VERSION` file at all. `kit.json` → `contract_version`
  remains the primary read and is always present, so the fallback should never fire — but as
  written it can only read the wrong number or nothing. (Section 1, Q1.)

- **Descend one level into `cinna-contract/` when extracting `contract.tar.gz`.** The archive
  has one top-level directory, mirroring how `kit.tar.gz` roots at `cinna-kit/`. Your
  handover describes the extracted tree, not the archive. (Section 14.)

- **Replace `resources/cinna-kit-contract/` wholesale with the published contract tarball —
  `layout.json` included, not the `templates/` subtree alone.** Every file in your authored
  `templates/` differs from ours, and it is missing `.claude/settings.local.json`,
  `.python-version`, `pyproject.toml` and `workspace_requirements.txt`, and has
  `files/README.md` where we have `files/.gitkeep`. Ours ship. Byte-identical scaffolds
  (§9.1) then follow from a shared input rather than from two teams converging by hand.

  **The consequence is sharper than the ask, so take it as the headline.** The two bundled
  `layout.json` files differ **while both declare `"contract_version": "1.0.0"`**. Re-derived
  now by loading both files: ours carries **41** exclude patterns and yours **32**; yours is
  a strict subset, and the nine it lacks are `publications.json`, `temp/`, `credentials/`,
  `**/*.env`, `__pycache__/`, `.mypy_cache/`, `**/.mypy_cache/`, `.ruff_cache/` and
  `**/.ruff_cache/`. Yours also carries no `secret_files` block at all, and types
  `desktop_owned` as `string[]` where ours is `object[]`. Same declared version, different
  content — **so the version number cannot be used to tell the two apart.**

  **Therefore: every parity claim in this document is a claim about a post-adoption desktop,
  and none of them holds against the build on your disk today.** A reader who takes them as
  current will conclude the contract is already met.

- **Write the contract's key names in `app-data/desktop.json`: `api_base_url` and
  `agent_token`, with the optional `chat_path`.** You currently write `localApiBaseUrl` and
  `agentToken` (`desktopStateService.ts:45,47`, read by `coerce` at `:112-113`). We did not
  add camelCase tolerance, so until this lands `kit.py chat` against a desktop-written folder
  reports "not connected". (Section 1, Q2.)

- **Implement the `chat` wire shape.** `kit.py chat Local/<slug> "<prompt>"` reads
  `<agent>/app-data/desktop.json`, then `POST {api_base_url}{chat_path or "/chat"}` with
  `Authorization: Bearer <agent_token>` and body `{"prompt": "<text>"}`. The response is
  newline-delimited JSON; we print the text of each line as it arrives and are tolerant about
  the key — `text`, then `content`, then `delta` — because you have not built this API yet. A
  line with `"type": "error"` goes to stderr and sets a non-zero exit. Connection refused,
  401, or any non-2xx exits non-zero with a one-line explanation, and it **never** falls back
  to role-play. The tolerant key handling and the `chat_path` default are a seam that lets you
  build the API without a second round-trip to us; they are documented here so they do not
  become accidental permanence.

- **Fix your scaffolder's handling of `KIT_VERSION` — this one is our fault, and it is
  section 5.** Your `MANIFEST_TOKENS` enumeration will find nothing to substitute for it,
  because the platform substitutes that token across every rendered member before delivery.
  Six of the seven are what is really waiting for your scaffolder.

- **Split `checkIdentity`'s two unparseable cases.** `checkContractCompatibility`
  (`contractVersion.ts:92`) returns `unknown` when either side is unreadable and
  `validator.ts:204-205` routes both to `manifest.contract_version.invalid`, so **the user is
  told their folder is broken when their app is stale.** One line, and it currently points
  every resulting support conversation at the wrong artefact.

- **Fix `asStringArray` so the `desktop_owned` object form parses** (`layout.ts:228`). Accept
  an entry that is either a string or an object carrying a `path`, normalised to one internal
  shape. Your `layout.test.ts:23` will go red against the shipped contract until this lands.
  (Section 3.)

- **Read the desktop state file's path from `layout.desktopOwned()` instead of the hard-coded
  constant.** `DESKTOP_STATE_FILE = 'app-data/desktop.json'` (`manifest.ts:150`) is used
  directly by `desktopStatePath` in `desktopStateService.ts`; `desktopOwned()` exists and has
  no behavioural reader. This pairs with the item above: parsing the object form is what makes
  reading it possible, and reading it is what makes parsing it matter.

- **Add `credentials/` to your `cloud_import_excludes`.** Your authored list lets
  `credentials/README.md` and `credentials/.env.example` through; ours excludes the directory
  wholesale. Without this the two `content_hash` walks cover different file sets and **never**
  match. This is not a preference: `credentials/README.md` collides with a platform-generated
  file that feeds the agent's system prompt (`update_credentials` overwrites it on every
  credential sync, and the prompt generator reads it), so a travelling kit README would inject
  local-machine dotenv instructions into a *cloud* agent's system prompt until the first sync
  silently replaced it. `credentials/` is excluded from every bundle snapshot anyway, so the
  files would not have survived into a published bundle either.

- **Add `.mypy_cache/`, `.ruff_cache/` and `temp/` to your exclude list.** All three were
  travelling. "Both sides upload a `.mypy_cache`" describes an agreement to do something bad
  in unison, not evidence the omission is safe; `temp/` is the worst of the three and the most
  likely to hold something the user did not mean to publish.

- **Take those additions in paired form — the root pattern *and* its `**/`-prefixed
  sibling — not the `**/`-only form.** A `**/`-prefixed directory pattern cannot match at the
  root. (Section 7.)

- **Add `__pycache__/` alongside your existing `**/__pycache__/`.** It is the one entry in
  your authored list that was never paired, so a root-level `__pycache__/` has been
  travelling.

- **Adopt the declared dotenv secret rule (`secret_files` in `layout.json`).** Enumerated
  suffixes leak the first one nobody thought of. The rule: a file is secret and never travels
  when its basename is `.env`, starts with `.env.`, or ends with `.env` — **unless** the
  basename ends with a declared allowed suffix (`.example`, `.sample`, `.template`). The block
  declares its own fail-safe: a clause a host cannot evaluate resolves toward secret — an
  unknown `match` clause counts as a hit, an unknown `unless` clause counts as a miss.

- **Apply `secret_files` to the *hashed* set, not only the copied set.** The natural
  implementation site is inside `isExcludedFromExport`, which feeds your hash — and
  implementing it only there shrinks your hashed set without shrinking ours, guaranteeing a
  mismatch on adoption. Our secret filter applies to the single walk, so copy set == hashed
  set == your set. The contract's `notes` state this as a MUST.

  **And test that adoption at the shape layer**, not the contents layer: feed your rule reader
  a rules array whose *entries* are the wrong kind of thing and assert the gate still
  withholds. Our own mutation table missed exactly this. (Section 9.)

- **Add `publications.json` to your `cloud_import_excludes`, root-anchored, as a plain
  string.** Never an object entry — `asStringArray` drops it without a sound — and never
  `**/publications.json`, which would withhold a nested `files/publications.json` that is an
  ordinary user file. Without it the ledger lands in your hashed set and not ours, and **the
  trigger is the first publish, on every folder.** (Section 6.2.)

- **Point every publication read site at `publications.json`, and move them together.**
  `validator.ts:512-540`, `scannerService.ts:420`, `src/shared/localAgents.ts:249`,
  `ReadOnlyCards.tsx:233-246`, and the type at `manifest.ts:122`. **Fixing only the validator
  is worse than useless** — it goes quiet while the card keeps rendering "never published".
  The checks are right and keep their severity; only the file they open moves. Keep
  `publications?` on the manifest type as tolerated-and-ignored, because a pre-split folder
  still carries it. (Section 6.1.)

- **Fix your schema's `publications[].content_hash` description.** It still describes the
  pre-secret-rule hash ("`cloud_import_excludes` applied"). A desktop implementing the hash
  from that description alone covers a different file set, so the two hashes **never agree,
  silently, with no error on either side**.

- **Drop the closed `enum` on `credentials[].type` in your authored schema.** (Section 4.)

  **These last two are the two deliberate divergences from your authored schema file, and
  they do not deserve equal billing.** The `content_hash` description is the urgent one: it
  degrades **silently and permanently** into "unpublished changes forever". The `enum` is a
  *loosening*, so a desktop still running the closed version merely over-rejects an unknown
  type — visible, and it fails safe. The generalisable point: a divergence that loosens a rule
  degrades noisily and recoverably; a divergence that changes **which files a hash covers**
  degrades silently and permanently.

- **Add the forbidden-value-key check to your credential validation, at error severity.** A
  credential slot carrying `value`, `secret`, `token`, `api_key`, `client_secret`,
  `private_key` or `credential_data` is a secret about to be committed. We kept ours an error
  rather than demoting it to match you. (Section 13.)

- **Keep `?` consuming one UTF-16 code unit in segment matching.** Yours is already correct
  and ours was not; this is on the list so neither side "simplifies" it later.

- **Agree a decoding for non-UTF-8 filenames, or exclude such paths from the contract.** This
  one needs a decision from both of us, not an implementation fix on either side: Python and
  Node decode such a name to different strings, so `content_hash` parity is unreachable for
  it whatever either side encodes with.

- **Normalise an exclude pattern before choosing its directory branch — and match what the other
  two hosts now do, which is strip it and warn.** Trailing whitespace in a pattern survives into
  the pattern body as a segment and matches nothing. This was a blind spot shared by all three
  hosts when section 7 was written; it no longer is. cinna-cli and cinna-core have both since
  adopted **strip-and-warn**: the pattern is read stripped, and a warning naming it is printed so
  the author learns their pattern was read differently from how they wrote it. Silent acceptance
  is the one behaviour none of us should have, because both of us hash the file set these patterns
  select. **You are the remaining host**, so this is now a matching change rather than a
  coordinated one — implement strip-and-warn rather than choosing your own resolution. (Section 7.)

- **Check your HTTP stack for the two token-egress properties in section 8** — a redirect
  carrying `Authorization` to another host, and a configured proxy carrying it to
  `127.0.0.1`. Both were demonstrated, not reasoned about, and the redirect leak's symptom is
  a chat that appears to have worked.

- **Compare `email` in every desktop token response — the exchange, the code exchange, and
  every refresh — against the account your user expected, and refuse the session when it
  differs.** A consent request naming no client can be approved by any authenticated holder of
  its nonce, and your app then redeems a code for the approver's account. The field makes that
  visible; your comparison is the only thing that prevents it. (Section 11, disclosure 2.)

- **Never tell a user that disconnecting a session or revoking the account token ends
  everything a linked session did.** An MCP OAuth credential such a session mints outlives
  both, is listed nowhere, and is revocable only by whoever holds its value — for up to thirty
  days. We are raising the fix separately; until it lands, your leak-response guidance must say
  revoke, then rotate. (Section 11, disclosure 1.)

- **Code the silent link against section 11's registered contract**, not against handover
  §8.2's "shape TBD": account token as bearer; optional `client_id`, else the three display
  fields; treat 403 as "drop this client id and fall back to the browser consent" and 429 as
  back-off, never retry-in-a-loop. The exchange is not role-gated and not machine-bound, by
  decision — do not describe it to users as either.

---

## Appendix — what we changed, in one place

| Area | Change |
|---|---|
| Contract identity | `layout.json` and `CONTRACT_VERSION` added at kit root; contract members are `kit.json`, `layout.json`, `CONTRACT_VERSION`, `CHANGELOG.md`, `schema/**`, `templates/**`. No second tree and no `contract/` subdirectory — the contract is a declared member list rendered from the one kit snapshot |
| Endpoints | `GET /agent-start/contract.tar.gz` and `GET /agent-start/contract/version` added (both also under `/api/agent-start`); `schema_version` removed from `GET /agent-start/version` |
| Desktop token exchange (§8.2) | `POST /api/v1/cli/account/desktop-token` built — account CLI token as bearer, desktop pair bound to `client_id`, `email` in the response, `origin="cli_exchange"` in App Sessions, revoked by disconnect or by the account token's cascade. Every desktop token response (exchange, code exchange, refresh) now carries `email`. Two disclosures not fixed here: an MCP OAuth credential minted by a linked session outlives every revocation control; a stranger's approval of a lazy-registration consent hands the redeeming app the stranger's session (section 11) |
| Manifest schema | Your authored schema adopted, with our `$id` kept. New properties `id`, `contract_version`, `runtime`, `created_at`; `required` down to `["name","slug","description"]`; top-level legacy-exemption `allOf`; `credentials[].type` enum dropped for `examples` plus a description; `publications` moved out to `schema/publications.schema.json` |
| Folder rules | One exclude list, in `layout.json` → `cloud_import_excludes` (41 patterns), replacing four separate mechanisms; new `secret_files` block declaring the dotenv rule as data; `desktop_owned` in object form carrying `contract_keys` |
| Ledger | `publications[]` lives in `publications.json` at the agent root, excluded root-anchored, schema pointed at from `kit.json` → `publications_schema` |
| `kit.py` | New `chat` verb; `new --description/--json` and the UPPER_SNAKE token set; `list` gained a `DESKTOP` column and reads the ledger for `CLOUD`; `validate` gates on `contract_version` and its severities align with yours; `export` reads `layout.json`, grew `--hash`, and no longer rewrites the manifest |
| Guides and templates | Default workshop path is now `~/Documents/CinnaAgents`; `Cloud/<host>/` per-instance workspaces; new `assistants/cinna-desktop.md`; `CHANGELOG.md` carries §4's Compatibility table verbatim |
