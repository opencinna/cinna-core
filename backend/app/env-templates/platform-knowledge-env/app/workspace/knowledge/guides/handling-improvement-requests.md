# Handling Improvement Requests

End-to-end walkthrough for turning a user's **improvement request** into an
actual fix — from the account workspace, with the `cinna improve` verbs.

An improvement request is a consent-gated, one-directional share: a user who was
chatting with an agent hit a bad answer and deliberately handed the *agent's
owner* a frozen snapshot of that one session plus the runtime context that
produced it. You are receiving **another person's conversation**. Treat it that
way — see [Step 7](#step-7--handling-the-data).

## When to use this

Read this whenever the user asks something like:

- *"Check for improvement requests and implement those."*
- *"Anything new in the improvement queue?"*
- *"Someone reported the CRM agent is asking for the same file twice — fix it."*
- *"Work through the open requests on my published bundle."*

If the user just wants to *see* what's pending, Steps 1–2 are the whole job.
Everything after that is for actually changing an agent.

## The loop at a glance

```
cinna improve list --status new        →  pick a request
cinna improve show <id>                →  read the detail + context block
cinna improve download <id>            →  extract the archive, read README.md
        ↓
establish ownership  (who can actually fix this agent?)
        ↓
decide autonomy      (implement now, or stop and ask?)
        ↓
fix  →  verify  →  (bundle: publish a new version)
        ↓
cinna improve status <id> completed --note "…"
```

## The CLI verbs

| Command | Backend endpoint | What it does |
|---|---|---|
| `cinna improve list [--status S] [--agent A]` | `GET /api/v1/cli/account/improvement-requests` | Table across every agent you own: id, agent, requester, version, date, status |
| `cinna improve show <id>` | `GET …/improvement-requests/{id}` | Full detail including the runtime `context` block |
| `cinna improve download <id> [--out DIR]` | `GET …/improvement-requests/{id}/archive` | Saves + extracts the ZIP into `improvements/<short-id>/` and prints the path |
| `cinna improve status <id> <status> [--note N]` | `PATCH …/improvement-requests/{id}` | Sets status (`new` / `in_progress` / `completed` / `declined`) and the resolution note |

The resolution note is **shown to the requester**. Write it for them, not for
yourself.

## Step 1 — Discover

```bash
cinna improve list --status new
```

Add `--agent <name>` to narrow to one agent. Statuses move
`new` → `in_progress` → `completed` | `declined`; there is no other transition to
learn.

Mark the one you pick up as in progress so a parallel session (or the user
watching the Configuration tab) doesn't grab it too:

```bash
cinna improve status <id> in_progress
```

## Step 2 — Read

```bash
cinna improve show <id>
cinna improve download <id>          # → improvements/<short-id>/
```

Read the extracted files **in this order**:

| File | What it carries |
|---|---|
| `README.md` | The reported problem verbatim, who reported it and when, which agent and which bundle version, a runtime-context table, whether the prompts diverged, and what is *not* in the archive |
| `prompts/README.md` | **Read this before the transcript.** Which prompt documents differ from the version you published, and the tool configuration |
| `prompts/*.md` | The agent's instructions **as they actually ran** on that install |
| `memory/` | The personal notes the runtime injected into every system prompt on that install (present only when the reporter included them) |
| `session/messages.md` | Human-readable transcript of the shared session |
| `context.json` | The structured runtime context — agent/bundle install flags, environment, SDK engine + effective model, plugin list, prompt hashes + divergence |
| `session/messages.json` | The same transcript, structured, when you need exact tool calls or ordering |
| `metadata.json` | The request row itself (id, status, timestamps, counts) |

> **The prompts in `prompts/` are the ones that ran — yours may not be.** A user
> can edit `WORKFLOW_PROMPT.md` inside their own install, and that edit stays in
> their copy. If `prompts/README.md` says a document diverged, reproduce and fix
> against the text in the archive, not against your own install. Diff it:
>
> ```bash
> diff improvements/<short-id>/prompts/WORKFLOW_PROMPT.md \
>      agents/<name>/workspace/docs/WORKFLOW_PROMPT.md
> ```
>
> Divergence reported as *unknown* means there was no installed bundle revision
> to compare against — it is not the same as "no changes".

Then — **before touching anything** — write yourself a short
**expected vs. actual**:

> *Expected:* the agent answers with the invoice total in one turn.
> *Actual:* it asks the user to re-upload the file it was already given, twice,
> then answers from the wrong month.

If you cannot write that pair from the archive, you do not yet understand the
request. Say so and ask the user rather than guessing at a fix.

A few things the archive deliberately does **not** contain: container logs,
uploaded file contents (descriptors only), the agent's scripts / knowledge files
/ app data, and any credential value (the transcript, the prompts and the memory
files are all scrubbed before they are stored). If the fix genuinely depends on
one of those, that is a *stop and ask* — see Step 5.

> **No `memory/` folder?** `README.md` says why, and the reasons are not
> interchangeable: *the reporter opted out*, *the environment was stopped*, *the
> container could not be read*, or *this install genuinely had no notes*. Only
> the last one lets you rule memory out as an explanation.

> **`snapshot_truncated: true`** means the snapshot hit the size cap and the
> **oldest** messages were dropped. The beginning of the conversation — often
> where the real instruction was given — may be missing. Say so in your summary
> instead of reasoning as if you have the whole session.

## Step 3 — Establish ownership

This is the step that gets skipped and it is the one that decides whether your
fix reaches anybody. **Where you fix an agent depends on which kind of install
you are holding.**

Two sources tell you:

```bash
cinna account agents          # → is_publisher_install, is_foreign_install per agent
```

and the archive's `context.json`:

```jsonc
"agent": {
  "is_bundle_install": true,
  "is_publisher_install": false,
  "bundle_id": "io.opencinna.cinna.a1b2c3d4",
  "installed_version": "1.3",
  "latest_version": "1.5",
  "update_pending": true
}
```

Note that `context.json` describes the **requester's** install (the one that
misbehaved). The target agent — the one the request landed on, and the one you
can act on — is named in `cinna improve show`. They are usually different rows.

### The three branches

**A. Standalone agent you own** — `bundle_uuid` is null, `is_foreign_install` is
false.

Fix it directly in the synced workspace:

```bash
cinna agent sync <name>
# edit agents/<name>/workspace/… , verify with cinna dev / cinna exec
```

Finish prompt changes per
[authoring-agent-prompts.md](authoring-agent-prompts.md) — including rewriting
the `description` if behaviour changed. Then close the loop (Step 6). Done.

**B. Publisher install of a bundle you publish** — `is_publisher_install: true`.

This is the copy that becomes the next published version, and it is the **only**
copy you should ever change.

1. Fix and verify here, exactly as in branch A.
2. Tell the user to **publish a new version** from the agent's Bundle tab.
3. Explain the consequence, because it decides when the reporter actually
   benefits:
   - installs on **automatic** update mode converge on their own;
   - installs on **manual** mode stay on their current version until their owner
     clicks Update.

> **Never attempt to edit a consumer's install.** You cannot reach it, and a fix
> that lived only in someone's installed copy would be wiped by the next bundle
> update anyway. The fix ships as a version; there is no other route.

**C. A consumer install you own** — `is_foreign_install: true` (bundle-owned,
`is_publisher_install: false`).

This happens when the publisher was unavailable — a deleted publisher install,
or an ownerless git-imported bundle — so target resolution fell back to your own
copy. `context.json` records `"fallback_reason": "publisher_unavailable"`.

> **Any local change here is overwritten by the next bundle update.** A consumer
> install has a publisher-managed workspace: it can be enabled, disabled, and
> run, but it is not a sync, exec, or build target — `cinna agent sync` will
> refuse it. Even if you found a way around that, the next update replaces the
> workspace wholesale.

Do not try to patch it. Tell the user plainly: this request landed on their own
copy because the publisher could not be reached, the fix belongs upstream, and
the useful action is **forwarding the feedback to the publisher**. If they want
to own the agent's behaviour themselves, that is a separate decision (fork or
rebuild as a standalone agent) — ask, don't assume.

## Step 4 — Diagnose against the runtime context

The context block exists so you don't fix the wrong layer. Before editing a
prompt, check whether the report is explained by the environment instead:

| Context signal | What it often means |
|---|---|
| `update_pending: true`, `installed_version` behind `latest_version` | Already fixed upstream. Verify against the latest version before writing new code |
| `effective_model` is a small/fast tier | Instruction-following failures may be a model-tier issue, not a prompt bug |
| `sdk.effective_engine` differs from what you build against | Behaviour differences between engines are real; reproduce on the engine in the report |
| `image_stale: true`, or `current_image_tag` ≠ `expected_image_tag` | The env is running an old image — the workspace may not be what you think |
| `critical_state: true` | The environment was degraded during the session; the transcript may show infrastructure symptoms, not agent defects |
| `plugins: []` where you expected plugins | A plugin failed to install; that is an env problem, not a prompt problem |
| `prompts.diverged: true` | The user is not running your text. Reproduce against `prompts/`, and consider whether your published prompt invited the edit |
| A tool in `sdk_tools` but not in `allowed_tools` | The user was prompted for permission on every use — a common cause of a run that looks stuck or repetitive |
| `memory/` explains a behaviour the prompts don't | A personal note is steering the agent. Fix by making the prompt robust to it, never by asking the user to delete their notes |

Say which layer you concluded it is. "Fixed the prompt" for what was a stale
image is a fix that never lands.

## Step 5 — Decide autonomy

Implement immediately **only when all** of these hold:

- [ ] The change is clearly **within the agent's existing purpose**.
- [ ] It is **localized to prompts or scripts already in the workspace**.
- [ ] It touches **no credentials** and **no external systems**.
- [ ] It does **not change the agent's published contract** — A2A skills,
      `agent_api` endpoints and `policy.yaml`, schedules, or bundle credential
      specs.

Otherwise **stop and ask the user.** In particular, ask when the request:

- deviates from the agent's stated purpose (it is a new feature, not a defect);
- is ambiguous — two plausible readings lead to different agents;
- is disproportionately large for a single reported session;
- changes anything another party already depends on (a contract, above);
- would need a credential, a new integration, or data you don't have;
- is best answered by *declining* — the behaviour is correct and the report is a
  misunderstanding.

Asking is not a failure mode here. One user's session is a single data point;
the owner is the one who knows whether it generalizes.

## Step 6 — Close the loop

Every request you touch gets a terminal status. The note is read by the person
who filed it:

```bash
cinna improve status <id> completed \
  --note "Fixed in v1.6 — the agent now reuses the already-uploaded file instead of re-asking. Update your install to pick it up."

cinna improve status <id> declined \
  --note "This is working as designed: the agent asks for confirmation before sending. You can skip it by saying 'send without confirming'."
```

Good notes name what changed and what the requester should do next (update,
re-run, nothing). For a bundle fix, say the **version** the fix ships in — that
is the only thing that tells them whether they have it yet.

Don't mark `completed` on a bundle fix that is still sitting unpublished in the
publisher install. Until the version is out, it is `in_progress`.

## Step 7 — Handling the data

The archive is **another person's conversation**, shared once, under an explicit
consent screen that told them exactly what would be included. Hold to the same
bargain:

- **Never copy it into an agent's workspace.** Not into `agents/<name>/`, not
  into `docs/`, not as a test fixture. The workspace syncs into a running
  container and, for a publisher install, into a published bundle. This applies
  with particular force to `prompts/` and `memory/`, which *look* like files
  that belong in a workspace and are not: `prompts/` is the consumer's copy of
  your documents, and `memory/` is their personal notes. Read them, diff them,
  never adopt them.
- **Never commit it.** Not to the agent's git-backed workspace, not to any repo,
  not "temporarily".
- **Never paste a secret-looking value anywhere** — a token, key, cookie,
  connection string, or anything shaped like one. The snapshot is scrubbed on
  the way in, but scrubbing is a safety net, not a guarantee. If you see one,
  don't echo it into a file, a commit message, a prompt, or the resolution note;
  tell the user it appeared.
- **Quote sparingly.** Paraphrase the problem into the fix. A prompt or comment
  should describe the *failure mode*, never carry the reporter's data.
- **Delete the local copy when you're done:**

```bash
rm -rf improvements/<short-id>
```

If you are unsure whether something from the archive may be reused, the answer
is no.

## Common pitfalls

- **Fixing the consumer install.** Branch C above. The change is invisible to
  everyone and gone at the next update. Check the install flags first, always.
- **Fixing the publisher install and stopping there.** The fix exists but no one
  has it until a new version is published. Publishing is part of the fix.
- **Marking `completed` at merge time rather than at publish time.** See Step 6.
- **Treating a truncated snapshot as the whole session.** Check
  `snapshot_truncated` before concluding the user never gave an instruction.
- **Rewriting the agent around one report.** A single session is one data point.
  Localized fix, or ask.
- **Prompt-fixing an environment problem.** Run through the Step 4 table before
  editing prompts.
- **Debugging against your own prompts.** If `prompts/README.md` reports
  divergence, your install is not the one that failed. Diff first.
- **Copying a memory file into the agent.** It is one person's personal note.
  Make the prompt robust to it instead.
- **Leaving the archive behind.** `improvements/` is not a place to accumulate
  other people's conversations.
