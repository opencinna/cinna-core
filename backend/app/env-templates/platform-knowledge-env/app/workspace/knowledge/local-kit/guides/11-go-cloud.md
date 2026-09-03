# 11 — Going cloud

## Read this when

The user asks to move an agent to {{INSTANCE_NAME}} ({{PLATFORM_URL}}), or the agent
needs something a laptop cannot give: 24/7 unattended runs, email or chat channels,
sharing with other people, a webapp, or an always-on API.

Everything before this point needed no account. From here on it does.

Work through the preconditions in order. Each one has a check and a recovery. Do not
skip ahead: a failure in step 8 usually means a precondition was assumed.

## Preconditions

### 1. `uv`

```bash
uv --version
```

Missing? Install it from `https://docs.astral.sh/uv/` (the site's one-line
installer). Do not install it silently — ask the user first.

### 2. cinna-cli, at least `{{MIN_CLI_VERSION}}`

```bash
cinna --version
```

Install or upgrade:

```bash
uv tool install {{CLI_INSTALL_SPEC}}
uv tool upgrade cinna-cli
```

If the version is below `{{MIN_CLI_VERSION}}`, upgrade before continuing. Some verbs
below may not exist in an older build — step 11 is the fallback that uses only
long-standing verbs.

### 3. Mutagen

The CLI uses Mutagen for live file sync. If it is not installed, `cinna` offers to
install it on first use — let it, or install it yourself from the Mutagen site.

### 4. An account on {{PLATFORM_URL}}

Sign up at {{SIGNUP_URL}}; sign in at {{LOGIN_URL}}. Two things commonly block a
brand-new account:

| Symptom | Meaning | Fix |
|---------|---------|-----|
| Agent creation refused, email not confirmed | The instance gates building until the address is confirmed | Open the confirmation link in the signup email |
| `403` on create / schedule / status commands | The account lacks the builder role | An admin grants the `agent-developer` role |
| `404` on an agent you believe exists | Not yours, or wrong workspace | `cinna account agents --all` |

`404` rather than `403` is deliberate on this platform: it does not confirm that
something exists to someone who may not see it.

### 5. Create this instance's workspace under `Cloud/`

`Cloud/` holds **one account workspace per platform instance**, each in its own folder
named after that instance's host. An account on two instances is two accounts — two
tokens, two sets of agents — so they never share a folder.

From the workshop root, naming the host of {{PLATFORM_URL}} as the folder:

```bash
cd ~/Documents/CinnaAgents
cinna login {{PLATFORM_URL}} --dir Cloud/<host>
```

The CLI prints a code and opens the browser; the user clicks **Authorize** while
signed in. Check `cinna login --help` first if your CLI names the target directory
differently.

**Run this from the workshop root, and always name `--dir`.** `cinna login` first
looks for an account workspace at or above the current folder: run from inside a
`Cloud/` that already holds an older flat workspace, it finds that one, refreshes its
token in place and ignores `--dir` with a warning. From the root there is nothing above
to find, so the folder you named is the folder you get. If your CLI prompts for a name
instead, its own suggestion is the host with the dots collapsed to underscores
(`acme_example_io`) — accept that or type the host. No tool reads the name; one folder
per instance is the rule that matters.

Afterwards `Cloud/<host>/` is a full account workspace: `.cinna/account.json` (it holds
the account token, and the CLI writes it `0600`), its own `CLAUDE.md`, an `agents/`
directory, and a `context/` folder with the platform's own documentation — including a
copy of this kit under `context/local-kit/`. **Inside that workspace, its `CLAUDE.md`
wins.**

Check it landed:

```bash
uv run .cinna-kit/tools/kit.py list
```

It prints a `Cloud/<host>/` block saying no agents are synced yet. A workshop from
before this layout, whose workspace sits directly in `Cloud/`, still works and is still
listed — leave it where it is and add further instances beside it.

### 6. Choose the target workspace (optional)

```bash
cinna account user-workspace list
cinna account user-workspace --activate=<id>
```

Only relevant if the user keeps several workspaces. Skip it otherwise.

### 7. The agent must validate

```bash
uv run .cinna-kit/tools/kit.py validate Local/<slug>
```

Blocking: a real `description`, at least one `example_prompt`, a non-empty workflow
prompt with no leftover template tokens, a manifest that matches the schema, and no
tracked secrets. Fix every error. Do not import a red agent — the import creates
platform objects and you will be cleaning up rather than iterating.

Run the secret sweep from `10-testing-locally.md` too.

## The import

### 8. Import

Every command from here runs inside the workspace from step 5, so the workshop is two
levels up — mind the `../../`.

```bash
cd Cloud/<host>
cinna agent import ../../Local/<slug>
```

It prints each step and is idempotent — rerun it with `--update` after a failure
rather than starting over. `--dry-run` prints the plan without writing anything;
prefer it the first time.

What it does: creates the agent, writes the prompts and metadata, syncs the
workspace, copies the tree, pushes it, creates credential drafts, creates schedules,
sets the status refresh command, and stamps a `cloud` block into `cinna-agent.json`.

Two facts about that last pair, because this is where the CLI and this kit have
drifted apart:

- **The CLI applies its own exclude list, not the contract's.** It looks for
  `cloud_import.exclude` in `.cinna-kit/kit.json`; that key no longer exists, so it
  falls back to its built-in defaults and says so — watch for `Exclusions: built-in
  default list` in its output. The contract's list lives in `layout.json` as
  `cloud_import_excludes` and withholds more. See the comparison at the end of this
  guide before you trust either.
- **The `cloud` block is that CLI's record, and the schema deprecates it.** This kit's
  publication ledger is `publications.json` beside the manifest, which `kit.py` reads
  first and the `cloud` block only as a fallback. Do not delete what the CLI wrote: a
  later `cinna agent import --update` resolves the existing agent by the `agent_id` in
  that block, and clearing it makes the next import create a second agent.

**`credentials/` is never copied.** The CLI prints one setup URL per credential.
Give those links to the user and let them enter the secrets in the browser. Do not
offer to paste values for them; do not read `.env` to "help".

If the CLI does not have `agent import`, go to step 11.

### 9. Verify

```bash
cinna chat --agent <slug> "<the agent's first example prompt>"
```

A real answer means prompts, workspace and credentials all landed. An answer that
complains about a missing credential means step 8's setup URLs are still unopened.

From here the **cloud copy is the live one**:

```bash
cd agents/<slug>
cinna dev            # live sync while you iterate
```

Decide with the user, out loud, which copy is authoritative from now on. Normally
the cloud one is, and `Local/<slug>` becomes an archive. If they want to keep
experimenting locally, they must re-import with `--update` afterwards — and know
that anything changed in the cloud in the meantime is overwritten.

### 10. Optional: publish as a bundle

If the agent should be installable by other people, read
`context/platform/agents/agent_bundles/agent_bundles.md` inside `Cloud/<host>/`. Do not
publish anything without the user explicitly asking.

## 11. Manual fallback (no `agent import`)

Every step below uses long-standing verbs. Run them from inside `Cloud/<host>/`, the
workspace created in step 5 — which puts the workshop two levels up.

```bash
# 1. Create the agent (name and description from cinna-agent.json)
cinna agent create <slug> --description "<description>"

# 2. Sync it down as a standard per-agent workspace
cinna agent sync <slug>
```

```bash
# 3. Write the prompts and metadata in ONE bulk write.
#    Build agents/<slug>/prompts.json from cinna-agent.json + the docs/*.md files:
#    description, workflow_prompt, entrypoint_prompt, refiner_prompt,
#    router_trigger_prompt, example_prompts.
cinna api PUT agents/<agent_id> --data @agents/<slug>/prompts.json
cinna agent show <slug> --prompts        # verify what landed
```

```bash
# 4. Copy the tree, applying the contract's exclude list — layout.json's
#    `cloud_import_excludes` plus its `secret_files` rules.
#    The manifest is copied UNCHANGED: export never rewrites it, so nothing
#    here clears a `cloud` block. Recording the publication is the last step,
#    after step 9, and it is yours to do.
uv run ../../.cinna-kit/tools/kit.py export ../../Local/<slug> --to agents/<slug>/workspace

# 5. Push it
cinna sync push
```

```bash
# 6. One credential draft per manifest slot. NO VALUES on the command line.
cinna account credentials create --name "<slot name>" --type <platform type>
cinna account credentials share-with-agent <cred_id> --agent <slug>
#    Then send the user to the platform UI to fill each one in.
```

```bash
# 7. One call per schedule in the manifest
cinna agent schedule create <slug> \
  --name "<name>" --cron "<cron_string>" --tz "<timezone>" \
  --prompt "<prompt>"
# script_trigger instead:
cinna agent schedule create <slug> \
  --name "<name>" --cron "<cron_string>" --tz "<timezone>" \
  --type script_trigger --command "<command>"
```

```bash
# 8. Status refresh command
cinna agent status set-command <slug> "/run:status"

# 9. Verify, as in step 9 above
cinna chat --agent <slug> "<first example prompt>"
```

Finally, record the publication. `cinna agent import` does this for you; on this
route it is yours, and it goes in **two** places because two different readers need
it.

**1. `publications.json`, beside `cinna-agent.json`** — this kit's ledger, and what
`kit.py list`, `kit.py validate` and the `go_cloud` rung read. Create it if it does
not exist; one entry per instance, matched later by `platform_url`:

```json
{
  "publications": [
    {
      "platform_url": "{{PLATFORM_URL}}",
      "agent_id": "<agent_id>",
      "workspace": "Cloud/<host>",
      "imported_at": "<ISO-8601 UTC timestamp>"
    }
  ]
}
```

`schema/publications.schema.json` carries the full shape, including the optional
`content_hash` a publishing tool writes to tell whether the instance is behind. The
file never travels to the cloud.

**2. The manifest's `cloud` block** (`platform_url`, `agent_id`, `imported_at`) — the
schema marks it deprecated and `kit.py` reads it only as a fallback, but it is still
what `cinna agent import --update` resolves the existing agent by. Write it too, and
keep it, until the CLI reads the ledger.

## The two routes do not exclude the same paths

`kit.py export` applies the contract's list: `cloud_import_excludes` in `layout.json`,
plus the `secret_files` rules that catch every dotenv shape (`.env`, `.env.<suffix>`,
`<name>.env`) rather than an enumerated few.

`cinna agent import` applies its own built-in list, because the `cloud_import.exclude`
key it looks for in `kit.json` no longer exists: `credentials/`, `.venv/`, `.claude/`,
`AGENTS.md`, `CLAUDE.md`, `app-data/`, `temp/`, `__pycache__/`, `*.pyc`, `.git/`,
`.DS_Store` — with `credentials/` and `app-data/` enforced whatever a kit says.

The contract's list covers everything in that list and more: `README.md`, `Makefile`,
`publications.json`, `.gitignore`, `.cursor/`, `.vscode/`, `.idea/`, `venv/`,
`node_modules/`, the `.mypy_cache/` and `.ruff_cache/` tool caches, `Thumbs.db`, any
`credentials.json` at any depth, and private key material (`*.pem`, `*.key`, `*.p12`).
So the CLI route pushes more of your folder than an export would. It is not careless
about secrets — it refuses the whole import if the copy plan reaches anything under
`credentials/` or `app-data/`, or a file named exactly `.env`. What it does not know
is the **suffixed** dotenv shapes: a `.env.local`, `.env.prod` or `staging.env` sitting
outside `credentials/` passes both its exclude list and that check. Those are precisely
what `layout.json`'s `secret_files` rules exist to catch, and `kit.py export` applies
them. If you took the CLI route, sweep for those yourself before pushing.

## If something fails halfway

Every step is idempotent by name or id: the agent by slug, credentials by name,
schedules by name. Rerun with `--update`. Do not create a second agent to "start
clean" — you will end up with two, and the platform resolves by id, not by name.

## Done when

- `cinna account agents` lists the agent.
- `cinna agent show <slug> --prompts` shows the real description, workflow prompt
  and example prompts.
- `cinna chat --agent <slug> "<example prompt>"` returns a correct answer.
- Every credential slot exists on the platform and the user has filled it in.
- Every manifest schedule exists (`cinna agent schedule list <slug>`).
- `cinna agent status show <slug>` shows a snapshot and the refresh command.
- `credentials/` was not copied — nothing under `agents/<slug>/workspace/credentials/`
  contains a secret.
- The publication is recorded: an entry in `publications.json` for this instance, and
  the `cloud` block in the local `cinna-agent.json` that the CLI resolves updates by.
  `uv run .cinna-kit/tools/kit.py list` shows the agent with `CLOUD` = `yes` and a
  `go_cloud` rung.
- The user has been told, explicitly, which copy is now authoritative.
