# Skills That Need a Credential

End-to-end walkthrough for building a **skill whose script needs a credential**
from the account workspace: pick the slot, declare it in `SKILL.md`, read it in
the script, tell the model when to run the script, then verify and test it —
without opening the browser for anything but the secret itself.

## When to use this

Read this whenever a skill ships a script that calls something needing a token, a
login or a connection — an API key, a mailbox, an `agent_api` producer. A skill
made only of prompt text needs none of this.

## Concepts in one screen

- **Skill** — `skills/<name>/SKILL.md` plus optional `scripts/`, `references/`,
  `assets/`. The engine loads `SKILL.md` on demand; the script runs only when the
  model decides to run it.
- **Slot** — a credential's `service_uri`: a non-secret id the skill names and
  the script looks the credential up by. It is public (it ships in the catalog)
  and says nothing about the credential's type.
- **A declaration carries no secret.** `SKILL.md` names slot + type; publishing
  resolves where installers get the credential, installing provisions it.
- **A missing skill credential is a warning, never a block.** The agent keeps
  working; the skill's Addons row turns amber, and the script fails with a
  message naming the slot and the fix. The credential is only exercised when the
  script runs — so a skill that is never run never proves its credential works.

## Before you start

1. **The environment has the slot helpers.** They live in the container's core,
   which a *rebuild* copies in from the template — a restart re-runs the old copy.
   Check:

   ```bash
   cinna exec --agent <agent> python3 -c "from core.cinna_api import credentials; print(hasattr(credentials, 'require_slot'))"
   ```

   `False` ⇒ `cinna sync push --agent <agent>`, then `cinna agent rebuild-env <agent>`
   (a few minutes; it refuses to run over unsynced local changes). Until then use
   the `credentials.json` fallback in Step 3.
2. **The workflow prompt does not forbid running skill scripts.** Lines like
   "you have no scripts" or "never run anything" win over a skill's instructions
   on small models. A rebuilt environment's own conversation prompt already says
   running a loaded skill's script is expected and that a script error is
   relayed verbatim — but that ships in core (Before you start, step 1), so an
   environment not yet rebuilt does not have it, and a workflow prompt can still
   contradict it in wording. Check with `cinna agent show <agent> --prompts`.
3. **Your context package is current** — `cinna account refresh-context`.

## Step 1 — Find or assign the slot

Pick a slot that names the service, e.g. `api.weather.example`. Then make sure a
credential linked to the agent carries it:

```bash
# Every credential you own, with its SLOT column (never secrets)
cinna account credentials list

# An existing credential: set its slot, then attach it to the agent
cinna account credentials update <credential_id> --service-uri api.weather.example
cinna account credentials share-with-agent <credential_id> --agent <agent>

# No credential yet: draft one with the slot and attach it in one step
cinna account credentials create --name "Weather API" --type api_token \
  --service-uri api.weather.example --agent <agent>
```

The CLI never reads or writes a secret. A drafted credential shows as needing
setup until the user fills it in on the agent's Credentials tab. Link, slot and
fill changes reach a running environment's `credentials/credentials.json` on
their own; a stopped one picks them up at its next start.

`cinna agent show <agent>` lists each linked credential with its slot.

## Step 2 — Declare the slot in `SKILL.md`

```yaml
---
name: weather-report
description: Current conditions and a short forecast for a city. Use when the user asks about the weather or wants a forecast saved to a file.
credentials:
  - slot: api.weather.example
    type: api_token
    description: API token the forecast script sends to the weather service.
---
```

- Use the **block-list** form above. Inline mappings
  (`credentials: [{slot: ..., type: ...}]`) are not parsed.
- `slot` and `type` are required; `description` is optional (≤ 1024 chars).
- `type` must be a real credential type (`cinna account credentials types`);
  `mcp_provider` is refused — it never reaches `credentials.json`.
- At most 20 slots, unique within the skill; quote a slot made only of digits.
- A malformed block is the error `invalid_credentials`: the skill disappears
  from the engine's index and cannot be published.

## Step 3 — Use the slot in the script

```python
# skills/weather-report/scripts/forecast.py
import sys

from core.cinna_api import CredentialMissing, credentials

SLOT = "api.weather.example"   # keep in step with SKILL.md
TYPE = "api_token"

try:
    cred = credentials.require_slot(SLOT)
except CredentialMissing as exc:
    print(f"error: {exc}", file=sys.stderr)   # names the slot and the fix
    sys.exit(3)
if cred.get("type") != TYPE:
    print(f"error: the credential in slot '{SLOT}' is not an {TYPE}.", file=sys.stderr)
    sys.exit(3)

data = cred["credential_data"]
headers = {data["http_header_name"]: data["http_header_value"]}
# ... call the service, print the result — never the header value
```

- `require_slot` raises `CredentialMissing` with reason `not_linked` (nothing
  carries the slot) or `not_configured` (a placeholder nobody filled in), and a
  message written to be shown to the user as-is.
- `require_slot` does **not** check the type — the platform's readiness check
  does. Check it in the script, as above.
- It reads `credentials.json` fresh on every call; don't cache the result in a
  long-running process.
- For an `agent_api` slot use `credentials.agent_api_session(SLOT)`: a ready
  `requests.Session` with the token and caller-identity headers set.

**Fallback for an environment without the helpers** (Before you start, step 1):
load `credentials/credentials.json` and take the entry whose `service_uri`
equals the slot and whose `is_placeholder` is not `true`. Scripts may read that
file; the model in a conversation must not.

## Step 4 — Tell the model when to run the script

The script is only as reliable as the instruction to run it. In `SKILL.md`, say:

- **which requests must go through the script** — concrete triggers ("several
  items", "save it to a file", "the same result again"), not "when useful";
- the exact command, run from the skill folder;
- **"If the script exits with an error, relay its message verbatim in one line.
  Do not work around it"** — otherwise the model improvises the answer and the
  credential failure is never seen.

A rebuilt environment already says the general permission and the relay-error-
verbatim rule generically, in its own system prompt, for every skill that ships
scripts — `SKILL.md`'s job is the trigger specificity and exact command a
platform-wide sentence can't have.

If the workflow prompt restricts the agent's scope, add one line allowing the
loaded skill's scripts. Edit prompts as files, through one path:

```bash
cinna agent prompts pull <agent>     # → prompts/<agent-slug>/workflow.md, …
cinna agent prompts diff <agent>
cinna agent prompts push <agent>     # writes the config, refreshes the env's docs/*.md
```

## Step 5 — Push and verify readiness

```bash
cinna sync push --agent <agent>
cinna skills list <agent>        # one line per declared slot: ✓ ready, ! not usable yet
cinna skills refresh <agent>     # if the list shows an unreadable or stale index
```

An unusable slot reads `! api.weather.example (<reason>)` with a remedy line:

| Reason | Meaning | Fix |
|--------|---------|-----|
| `not_linked` | no linked credential of the declared type carries the slot | Step 1 |
| `type_mismatch` | the credential on the slot is another type (CLI-side label; the platform reports `not_linked`) | link one of the declared type, or fix `type` |
| `not_configured` | linked, but a placeholder nobody filled in | the user fills it on the Credentials tab |
| `access_revoked` | linked, but a share you no longer have access to | ask the owner, or link your own credential |

## Step 6 — Test

```bash
# Positive: the script itself
cinna exec --agent <agent> python3 /app/workspace/skills/weather-report/scripts/forecast.py

# Negative: point the helpers at a file that does not exist → not_linked message
cinna exec --agent <agent> env CINNA_CREDENTIALS_PATH=/nonexistent \
  python3 /app/workspace/skills/weather-report/scripts/forecast.py
```

Then test the **agent**, not only the script. `cinna exec` proves the script
runs; only a chat proves the model runs it:

```bash
cinna chat --agent <agent> "save tomorrow's forecast for Lisbon to a file"
```

Output is NDJSON. In the `message` events with `"role": "agent"`, the `events`
array holds the tool calls — look for the command running `forecast.py`. Send
requests that *must* go through the script, and send each **several times**:
small models are nondeterministic, and one passing run proves little. If the
command dies while the agent keeps working, `cinna chat --attach <session_id>`
waits for that turn and prints it.

## Step 7 — Ship

Publishing reads the **cloud** workspace, so push first:

```bash
cinna sync push --agent <agent>
cinna skills publish <agent> weather-report --dry-run   # version, package id, revision
cinna skills publish <agent> weather-report --visibility private
```

Each declared slot is resolved against the credential the publishing agent links
for it: `publisher` (your credential, sharing on — installers use it),
`template` (template sharing on, no secret left — installers get a copy to
fill), or `user` (anything else — installers bring their own). `user` is a
normal outcome. The credential does not have to be yours: an `agent_api`
connection shared with you whose owner turned on **Allow re-use inside bundles**
can be `publisher` in your own catalog skill too (installers get it through your
skill; it is released if you lose your own access). Any other credential you do
not own resolves to `user`. The web Share dialog shows the resolution per slot before you
publish.

For a bundle, publish a new bundle version from the publisher install instead.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `AttributeError: ... has no attribute 'require_slot'` or `ImportError: cannot import name 'CredentialMissing'` | environment core predates the slot helpers | `cinna agent rebuild-env <agent>` (restart is not enough); fallback in Step 3 meanwhile |
| `invalid_credentials` on the skill | frontmatter shape (inline mapping, unknown or `mcp_provider` type, duplicate slot, > 20) | fix the block per Step 2, `cinna sync push` |
| `! <slot> (not_linked)` although a credential is linked | the credential's slot text differs from the declared slot (a credential of another type on the right slot reads `type_mismatch`) | compare with `cinna account credentials list`; see Step 5 |
| Skill loads but the script never runs | workflow prompt forbids scripts or claims there are none; `SKILL.md` triggers too vague; small model; environment core older than the platform's skill-script guidance | fix the prompt (Step 4), sharpen `SKILL.md`, rebuild an old environment, re-test with several chats |
| Agent writes the answer itself after a script error | `SKILL.md` doesn't say to relay the error verbatim | add the rule from Step 4 |
| `cinna chat` exits before the agent finishes | turn outlived `--timeout`, or the environment was still starting after a rebuild | `cinna chat --attach <session_id>` |
| First `cinna sync push` reports conflicts | local and remote both changed | `cinna sync conflicts --agent <agent> --diff`; `cinna sync push --agent <agent> --force` when local is the truth |
