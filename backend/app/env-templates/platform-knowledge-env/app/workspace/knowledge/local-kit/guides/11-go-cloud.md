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

### 5. Turn `Cloud/` into an account workspace

From the workshop root:

```bash
cd ~/Documents/MyAgents
cinna login {{PLATFORM_URL}} --dir Cloud
```

The CLI prints a code and opens the browser; the user clicks **Authorize** while
signed in. Check `cinna login --help` first if your CLI names the target directory
differently.

Afterwards `Cloud/` is a full account workspace: `.cinna/account.json`, its own
`CLAUDE.md`, and a `context/` folder with the platform's own documentation —
including a copy of this kit under `context/local-kit/`. **Inside `Cloud/`, that
`CLAUDE.md` wins.**

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

```bash
cd Cloud
cinna agent import ../Local/<slug>
```

It prints each step and is idempotent — rerun it with `--update` after a failure
rather than starting over. `--dry-run` prints the plan without writing anything;
prefer it the first time.

What it does: creates the agent, writes the prompts and metadata, syncs the
workspace, copies the tree (honouring the exclude list in `kit.json`), pushes it,
creates credential drafts, creates schedules, sets the status refresh command, and
stamps the `cloud` block into `cinna-agent.json`.

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
`context/platform/agents/agent_bundles/agent_bundles.md` inside `Cloud/`. Do not
publish anything without the user explicitly asking.

## 11. Manual fallback (no `agent import`)

Every step below uses long-standing verbs. Run them from inside `Cloud/`.

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
# 4. Copy the tree, applying kit.json's cloud_import.exclude list.
#    kit.py does exactly this and clears the `cloud` block for you:
uv run ../.cinna-kit/tools/kit.py export ../Local/<slug> --to agents/<slug>/workspace

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

Finally, record the result in the local manifest's `cloud` block
(`platform_url`, `agent_id`, `imported_at`) so a later re-import knows what exists.

The excluded paths, in both routes: `credentials/`, `.venv/`, `.claude/`,
`AGENTS.md`, `CLAUDE.md`, `app-data/`, `temp/`, `__pycache__/`, `*.pyc`, `.git/`,
`.DS_Store`.

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
- The `cloud` block in the local `cinna-agent.json` records the platform and agent id.
- The user has been told, explicitly, which copy is now authoritative.
