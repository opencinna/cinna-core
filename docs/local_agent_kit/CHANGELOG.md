# Kit changelog

Conventions that changed between kit versions. Read this after every
`kit.py refresh` that reports a new version. Newest entry first.

Your current kit is version `{{KIT_VERSION}}`.

Entries are headed by the **contract version** they belong to — the number in
`CONTRACT_VERSION`, in `kit.json`'s `contract_version`, and in every manifest
this kit scaffolds. The kit version above is a different number answering a
different question: it moves with every guide, template and tool change, and it
gates nothing.

The contract version is a semantic version. **Major** bumps are breaking — a
folder role moved, or a manifest field changed meaning; a tool whose major is
older than the folder's must refuse to operate it and ask to be updated.
**Minor** bumps are additive and safe to ignore. See "Compatibility" below.

## 1.1.0 — skills are folders

Additive. Every existing agent folder is still valid and needs no change; a tool
built against 1.0.0 keeps operating a 1.1.0 folder, because the majors match.

### Added

- **`skills/` — one folder per skill, at the agent root.** A skill is one
  standalone capability, packaged as `skills/<name>/SKILL.md` (YAML frontmatter
  plus a markdown body) with optional `scripts/`, `references/` and `assets/`
  beside it. It is shaped like the open Agent Skills standard, which is what
  buys the behaviour the folder exists for: the engine reads only every skill's
  `name` and `description` up front and loads a body when that skill is actually
  invoked, so a capability can be as long as it needs to be without sitting in
  the prompt all the time. `layout.json` declares the folder with role `skills`
  and `survives_update: true`, and `templates/agent/skills/README.md` explains
  the layout in the scaffold. Two frontmatter fields are validated: `name`
  (`^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 characters, and it must equal the folder
  name) and `description` (1–1024 characters). Everything else in the
  frontmatter is passed through untouched. Caps: 50 skills per agent, 16 MB
  across `skills/`; a body over 64 KB is still shipped but flagged. A skill name
  may not collide with a platform command name (`files`, `files-all`, `run`,
  `run-list`, `skills`, `session-recover`, `session-reset`, `session-improve`,
  `webapp`, `rebuild-env`, `agent-status`). `skills/` is **not** in
  `cloud_import_excludes`: it is part of what the agent *is*, so it travels.

- **Two sections on designing around skills, and on publishing one.**
  `guides/08-knowledge-and-local-skills.md` gains both. *Designing around skills*
  turns the split table around for the design phase: an agent with several distinct
  internal workflows gets one skill folder per workflow by default, and a prompt
  that only routes between them; the tiebreak for a workflow that argues otherwise
  is separate trigger + separate output + reusable by another agent. *Publishing a
  skill* covers the other end — `cinna skills publish <slug> <name>`, or the
  **Addons** tab's **Share…** on the agent's page. Two facts that section exists to
  state: publishing reads the **cloud** agent's workspace, never this folder, so the
  agent must already be in the cloud (`guides/11-go-cloud.md`) and the local edit
  must have travelled there first — a revision is immutable, and publishing a stale
  copy cannot be undone, only appended to. A package is also **private by default**,
  so the bare command succeeds, prints a catalog URL and shares the skill with
  nobody — the guide hands over `--visibility public` (or `--visibility users
  --grant <email>`) and never the bare form. And the secret gate is filename-based
  only: nothing reads inside the files. `templates/agent/AGENTS.md` carries the rule
  in one line; the `knowledge` rung's `trigger` in `kit.json` (and its two
  restatements in `README.md`) widens to fire on a finished skill worth publishing,
  not only on three capabilities or a page of domain docs. Guidance only — no folder
  role, manifest field or validation rule moved, which is why this sits under
  contract 1.1.0 rather than bumping it.

### Changed

- **`docs/` is no longer where a local skill is documented.** Its `layout.json`
  role text drops "one doc per local skill" — `docs/` is the three
  document-backed prompts and the command catalog. **The `docs/skill_*.md` form
  is legacy, not removed**: those files still ship, still travel to the cloud,
  and are still read by an agent whose workflow prompt points at them. What they
  do not get is the engine's own progressive disclosure, because nothing but the
  prompt knows they exist. `guides/08-knowledge-and-local-skills.md` is rewritten
  around the folder convention and carries the five-step migration for one
  legacy doc — to be applied when you next touch that capability, never as a
  sweep. Half a migration is worse than none: a capability living in both
  `docs/skill_x.md` and `skills/x/SKILL.md` is two sources of truth, and the
  agent follows whichever it reads first.

## 1.0.0 — first contract release

The kit's conventions become a versioned **contract**, so a second host — Cinna
Desktop — can create, validate and export agent folders this kit accepts
unchanged, and the other way round. `kit.json`, `layout.json`,
`CONTRACT_VERSION`, this file, `schema/` and `templates/` are that contract; the
guides and `tools/` stay kit-only.

### Breaking

- **`contract_version` replaces `schema_version` as the compatibility gate.**
  Every manifest records the contract version it was scaffolded with, and
  `kit.py validate` compares its major against this kit's: the same major runs
  silently whatever the minor, a newer major is an error asking you to
  `kit.py refresh`, an older major is a warning saying the folder can be
  migrated. The integer `schema_version` is still *parsed* — a manifest carrying
  it is read, never rejected — but nothing branches on its value any more.
  Re-stamp a pre-1.0.0 manifest by adding `contract_version` and `id`.
- **A manifest carries a stable `id`** (UUID v4), written once at scaffold and
  never rewritten. Identity now survives a folder move or a slug rename, so
  chats, publications and cloud twins stay attached to the agent rather than to
  its path.
- **Identity is required of everything but a legacy folder.** `contract_version`
  and `id` are deliberately *not* in the schema's top-level `required`, because a
  legacy folder must still parse — legacy meaning "carries `schema_version` and
  neither of them". Such a folder gets one warning asking for a re-stamp;
  anything else missing them is an error. The rule is encoded in three places —
  the Compatibility table below, `schema/cinna-agent.schema.json`'s identity
  `$comment`, and the desktop's `checkIdentity()` — and a change to one is a
  change to all three.
- **`cloud` becomes `publications[]`, in a new sibling file.** One entry per
  instance the agent was published to — `{platform_url, agent_id, workspace,
  imported_at, updated_at, contract_version, content_hash}` — because a single
  `cloud` object cannot express an agent published to two servers. The ledger
  lives in `publications.json` beside the manifest rather than inside it: it is
  mutable per-instance sync state, not identity, and each entry records a
  `content_hash` **of the exported tree** — a tree the manifest is a member of,
  so a hash stored in the manifest could never match the tree it describes. The
  old `cloud` object is still accepted and still parsed; every manifest write
  migrates it, and a stale `publications` array with it, into the ledger — all of
  one key or none of it, never half. `validate` names either key until it moves.

### Added

- **`layout.json` — the folder model as data**, so every host reads the rules
  instead of hard-coding them: the role of each workshop and agent folder and
  whether a refresh may replace it (`survives_update`), the
  `cloud_import_excludes` list and the semantics its notes define, the
  `secret_files` rules, the `scaffold_ignore_files` dotless-to-dotted pairs, the
  `local_command_runner` rule that turns a cloud-first `python …` command into
  `uv run …` when the agent has a `pyproject.toml`, and `desktop_owned`. The
  contract's own version ships beside it in `CONTRACT_VERSION`.
- **Nothing that can hold a credential value travels or is committed.** One rule,
  kept identical in three places: `cloud_import_excludes` (what never travels),
  `templates/agent/gitignore` (what is never committed) and every validator's
  secret check. It covers `credentials.json` — what the platform injects at the
  agent root, and what `scripts/cinna_credentials.py` reads in the cloud, so a
  folder that has run there can otherwise carry live values home — plus any
  `.env`, `*.pem`, `*.key`, `*.p12` and `*.tmp`. What a glob cannot express is
  declared instead as `layout.json`'s `secret_files` rules: every dotenv shape
  (`.env`, `.env.<suffix>`, `<name>.env`) holds a value unless the name ends
  `.example`, `.sample` or `.template`. A host applies that rule to the file set
  it *hashes*, not only to the files it copies — a path withheld from the upload
  but counted in the hash moves the hash for a change that can never be
  published, which reads as unpublished changes forever.
- **Optional manifest `runtime`** — `{model, credential, permissions}`.
  `credential` is a *reference* (a credential type, or the name of a credential
  configured in the host), never a key or a value: one that looks like a secret
  is an error telling you to rotate it, because a value in the manifest is
  already in every copy, export and hash of the folder. Absent means "use the
  host's default runtime", which is what a freshly scaffolded agent does.
- **Optional manifest `created_at`** (ISO 8601, UTC), written by the scaffolder.
- **`app-data/desktop.json` is named as the single desktop-owned file.**
  `layout.json`'s `desktop_owned` block freezes exactly the keys another tool may
  read — `api_base_url`, `agent_token`, and an optional `chat_path` — and
  everything else in that file is Cinna Desktop's to shape. `kit.py` reads those
  keys and no others; it never writes the file, never commits it, and never
  prints its contents. It is git-ignored and excluded from cloud import already.
- **`kit.py chat <agent-folder> "<prompt>"`** — send one prompt to a locally
  running agent through Cinna Desktop's local API and stream the answer back.
  It is how you test an agent for real instead of role-playing it, so it **never
  falls back to role-play**: every failure exits non-zero with one line that
  could not be mistaken for the agent's answer. Nothing on any path prints the
  token or the URL. It reads `app-data/desktop.json`, so it works only while the
  desktop is running with this agent connected.
- **`publications.json`, with its own schema** (`schema/publications.schema.json`).
  Absent until the first publish, never travels to the cloud, and survives a kit
  refresh — it is the user's own record of where their agent went.

### Changed

- **`schema_version` leaves the `/agent-start/version` response, and `kit.json`
  with it.** `GET /agent-start/version` — the unauthenticated endpoint
  `kit.py refresh` polls — returned a `schema_version` key on every response. It no
  longer does, and the same field is gone from `kit.json`. The two moved in one
  change on purpose: a number served on the wire with no backing field in the
  shipped tree reads as a serving fault rather than as a deliberate removal, and
  sends a reader hunting something that is not broken. Nothing could have been
  computing from it — it was a constant, compiled into the payload, identical on
  every instance, version and request — and `contract_version` is what answers the
  version question now, on `/agent-start/contract/version`. **This is not the
  manifest's `schema_version`**, which merely shares the name: that field is live,
  it is the sole marker of a legacy folder, and it stays exactly as the Breaking
  entries above describe.
- **The cloud-import exclude list moves to `layout.json`.** It was `kit.json`'s
  `cloud_import.exclude`; it is now `cloud_import_excludes`, with its semantics
  written down beside it in `cloud_import_excludes_notes` and one matcher
  implementing them on every host: a pattern with a trailing `/` excludes that
  directory and everything under it, `*` and `?` match within one segment, `**`
  matches zero or more whole segments, and a pattern without a leading `**` is
  anchored at the agent root and must match the whole path. Alongside what it
  already excluded, the list gains the remaining editor directories (`.cursor/`,
  `.vscode/`, `.idea/`), the tool caches (`.mypy_cache/`, `.ruff_cache/`),
  `node_modules/`, `venv/`, `Thumbs.db`, git's own metadata (`.gitignore`,
  `.gitattributes`), `publications.json`, and the credential and key material
  named above. A directory is listed in **both** the root-anchored and the `**/`
  form on purpose: a `**/`-prefixed directory pattern cannot match at the root,
  so `**/.mypy_cache/` alone would miss the only one that ever exists.

  What that changes for an agent you already have:

  - `AGENTS.md` and `CLAUDE.md` were dropped by basename at any depth, so
    `knowledge/AGENTS.md` never reached the cloud. They are root-anchored now:
    the agent's own assistant wrapper still stays behind, and one you wrote
    inside `docs/` or `knowledge/` travels.
  - `README.md` and `Makefile` **at the agent root** are newly excluded — the
    platform provides its own, and catalogued commands resolve through
    `docs/CLI_COMMANDS.yaml` rather than through `make`. `docs/README.md` and
    `scripts/README.md` are untouched; that is what the anchoring buys.
  - `.gitkeep` is newly excluded at any depth, so an agent whose `files/` holds
    nothing else no longer creates an empty `files/` in the cloud workspace.
  - `*.pyc` is now `**/*.pyc` rather than a basename match — the same files, by
    a mechanism another host can implement from the list alone.
  - The export no longer drops a file merely because its name starts with
    `.env.`. That short-circuit also swallowed `.env.example`; the `secret_files`
    rule decides now, and it spares `.example`, `.sample` and `.template`
    explicitly.

  **Two deliberate divergences from the desktop's authored list**, recorded here
  because anyone comparing the two lists will otherwise read them as drift:

  - **`credentials/` stays excluded wholesale**, documentation included; the
    finer per-file list does not apply inside it. The platform generates its own
    `credentials/credentials.json` and `credentials/README.md` on every
    credential sync and feeds that README straight into the agent's system
    prompt — so a local `credentials/README.md` that travelled would inject
    local-machine `.env` instructions into a cloud agent's prompt until the first
    sync silently overwrote them. `credentials/.env.example` is excluded with it:
    on its own it buys nothing.
  - **The pattern `**/*.env` is added.** The authored list carries `**/.env` and
    `**/.env.local` but not `**/*.env`, so `prod.env` or `staging.env` at any
    depth would travel under it. The three dotenv globs are a belt-and-braces
    subset of the `secret_files` rule, kept for a host that has not implemented
    that block yet; the rule is the authority.

- **The export walk is the same walk on both hosts, and it refuses rather than
  guessing.** Symlinks are never followed and never listed, an excluded directory
  is never descended into, and a directory or file the walk could not read is
  named and stops the export — a complete-looking tree with a subtree missing
  from it yields a `content_hash` that reads "up to date" forever.
  `kit.py export --hash` prints that hash: it is the number a publication entry
  records.
- **The scaffold's tokens are UPPER_SNAKE, and the manifest is filled by
  substitution like every other template file.** The set is `NAME`, `SLUG`,
  `DESCRIPTION`, `ID`, `CREATED_AT`, `CONTRACT_VERSION` and `KIT_VERSION`, each
  written in the templates as its name between double braces. The template used
  to carry literal sample text (`"New Agent"`, `"new-agent"`) that the tool
  patched onto the parsed manifest afterwards; one substitution pass now fills
  the whole tree, escaping values as JSON string bodies inside `.json` members,
  which is what lets two hosts scaffold byte-identical folders. `kit.py new`
  gains `--description` (it fills the DESCRIPTION token) and `--json`, which
  prints `{path, slug, id, contract_version}` and nothing else.
- **The dotless-to-dotted ignore-file pairs are declared data.** They live in
  `layout.json`'s `scaffold_ignore_files`, per template tree, instead of being
  prose in this file — which meant a scaffolder could restore `gitignore` and
  miss `app-data/cache/gitignore`. The files still ship dotless, and
  `templates/agent/credentials/.gitignore` still keeps its dot on purpose.
- **`Cloud/` holds one workspace per instance, `Cloud/<host>/`.** An account on two
  Cinna instances is two accounts, so a cloud workspace is named by the host it
  belongs to — `Cloud/acme.opencinna.io/` — and `kit.py list` reports each of them
  under its own heading. **An existing flat `Cloud/` keeps working**: it is still
  recognised, still listed, and lists alongside per-instance workspaces in the same
  run, so nothing has to be moved. Two smaller changes ride along: the heading above
  a listed workspace now names which workspace it is, and the table gained a
  `DESKTOP` column saying whether Cinna Desktop has that agent connected. `CLOUD`
  now answers `yes` for a folder whose `publications.json` records a publication,
  where it used to read only the deprecated `cloud` stamp — the same question, asked
  of the file the answer moved into. `list` output is written for a person to read
  and has never been a declared contract surface; nothing should parse it.

## Pre-contract — the first kit

First published kit, before the conventions were versioned as a contract.

- **`uv` is the kit runtime.** Setup checks for `uv` and installs it when missing;
  the kit tool is always invoked as `uv run .cinna-kit/tools/kit.py …` (it carries
  PEP 723 inline metadata, `requires-python >= 3.10`), so the macOS system Python
  3.9 is never a blocker. A bare `python3` below 3.10 now prints the `uv` fix
  instead of a dead end.
- `cinna-agent.json` carried **`schema_version` `1`**. Every agent this kit
  scaffolded recorded it, and `kit.py` refused a manifest whose `schema_version`
  was higher than the one it understood. **Superseded by 1.0.0:**
  `contract_version` is the gate now and nothing branches on the integer; a
  folder carrying `schema_version` and neither `contract_version` nor `id` is a
  legacy folder — read, reported, and asked for a re-stamp.
- Local agent layout mirrors the cloud workspace one-for-one: `docs/`, `scripts/`,
  `knowledge/`, `files/`, `config/`, `credentials/`, `app-data/{storage,cache,uploads}/`.
- Prompts live in `docs/WORKFLOW_PROMPT.md`, `docs/ENTRYPOINT_PROMPT.md` and
  `docs/REFINER_PROMPT.md` — the same three files the platform reconciles.
- `docs/CLI_COMMANDS.yaml` is cloud-first: commands use `python scripts/x.py` with
  paths relative to the workspace root. The `Makefile` mirrors each command with
  `uv run` for local use.
- `scripts/cinna_credentials.py` is the portability shim: `credentials.json` in the
  cloud, `credentials/.env` locally, one `get_credential()` call either way.
- `scripts/update_status.py` writes `app-data/storage/STATUS.md` atomically with
  YAML frontmatter (`status`, `summary`, `timestamp`).
- The capability ladder in `README.md` is the discovery mechanism; nothing is added
  to an agent until its trigger fires.
- Ignore rules that exclude paths ship as dotless `gitignore` files
  (`templates/agent/gitignore`, `templates/agent/app-data/cache/gitignore`,
  `templates/root/gitignore`). `kit.py new` restores the dot in the created agent;
  when you install `templates/root/` by hand, copy `gitignore` to
  `<root>/.gitignore`. (Shipping them dotted would make them live ignore rules
  wherever the kit is stored, hiding scaffold files from that repository.)
  `credentials/.gitignore` keeps its dot — it names files no repository should track.

## How to read a future entry

Each release lists, in this order:

1. **Breaking** — a convention that makes an existing agent invalid. `kit.py validate`
   will flag it; the entry says how to migrate.
2. **Added** — new optional artefacts. Existing agents keep working untouched.
3. **Changed** — wording, defaults, guide reorganisation. No action needed.

A bump of the contract's **major** version is always a Breaking entry.

## Compatibility

| Folder vs. tool | Behaviour |
|-----------------|-----------|
| Same major | Run as-is, whatever the minor. |
| Folder major **newer** | Refuse to run it: "update the app" / "refresh the kit". |
| Folder major **older** | Migratable — apply the Breaking entries for the gap, which is what a "Migrate to contract N.0" action does. |
| No `contract_version` | Unknown. Treat as legacy, read it, ask for a re-stamp. |
