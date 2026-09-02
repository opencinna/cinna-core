# 04 — Credentials

## Read this when

The workflow calls any external system that needs a token, login, key or OAuth
grant. Read it at the moment the need appears — not earlier, and not after a script
is already reading a file directly.

## The hard rules

1. **Never print, echo, log or paste a credential value.** Not into chat, a commit,
   a report, `STATUS.md`, an error message or a debug line.
2. **Never read `credentials/.env` or `credentials/credentials.json` in the
   conversation.** Only scripts read them, and only through the shim.
3. `credentials/` is **never** copied to the cloud, exported, or pushed anywhere.
4. If a credential is missing, name the slot and stop. Never guess, never substitute
   a test value.

If the user pastes a secret into the chat, tell them to put it in
`credentials/.env` instead, and do not repeat it back.

## The three moving parts

| Part | File | Contains |
|------|------|----------|
| **Declaration** | `cinna-agent.json` `credentials[]` | slot name, platform type, description, `env_prefix`, `fields`. No values. |
| **Values** | `credentials/.env` (git-ignored) | the actual secrets, locally only. |
| **Documentation** | `credentials/README.md` | redacted description of each slot and what it is for. |

Plus `credentials/.env.example`, which is committed and lists variable names with
empty values.

## 1. Declare the slot

```json
{
  "name": "billing-inbox",
  "type": "email_imap",
  "description": "Read-only IMAP access to the billing mailbox.",
  "env_prefix": "BILLING_INBOX_",
  "fields": ["host", "port", "login", "password", "is_ssl"]
}
```

`env_prefix` must be upper case and end with `_`. The local variable name for each
field is `<env_prefix><FIELD IN UPPER CASE>` — so `BILLING_INBOX_HOST`,
`BILLING_INBOX_PORT`, and so on.

## 2. Pick the platform type

Use one of the platform's credential types, so the slot maps 1:1 when the agent goes
cloud. These are the values `type` may take:

| Type | Fields | Notes |
|------|--------|-------|
| `email_imap` | `host`, `port`, `login`, `password`, `is_ssl` | Reading mail. |
| `email_smtp` | `host`, `port`, `username`, `password`, `from_email`, `use_tls`, `use_ssl` | Sending mail. |
| `odoo` | `url`, `database_name`, `login`, `api_token` | Odoo XML-RPC / JSON-RPC. |
| `api_token` | `api_token_type`, `api_token_template`, `api_token` | Generic HTTP API. `api_token_type` is `bearer` or `custom`; the template carries the header shape. |
| `google_service_account` | the service-account JSON fields (`project_id`, `private_key`, `client_email`, …) | Server-to-server Google access. |
| `ssh_key` | `public_key`, `private_key`, `fingerprint`, `key_type` | SSH / git access. |
| `gmail_oauth`, `gmail_oauth_readonly` | OAuth tokens | **Cloud-only.** |
| `gdrive_oauth`, `gdrive_oauth_readonly` | OAuth tokens | **Cloud-only.** |
| `gcalendar_oauth`, `gcalendar_oauth_readonly` | OAuth tokens | **Cloud-only.** |

**OAuth types cannot be filled locally.** There is no local browser consent flow and
no token refresh. If the agent needs one, either use `google_service_account`
locally, or build against a fixture and declare the OAuth slot for the cloud step,
telling the user plainly that this part only works after import.

## 3. Fill the values

```bash
cp credentials/.env.example credentials/.env
```

Then let the **user** fill in `credentials/.env`. Add the variable names to
`.env.example` yourself (names only, values empty) so the user knows what to enter.

Verify it cannot leak:

```bash
git check-ignore -v credentials/.env    # must print a matching ignore rule
git status --short credentials/         # must never list .env
```

The scaffold's `.gitignore` already covers it. If the check fails, fix the ignore
rules before continuing.

## 4. Use it from a script

Always through `scripts/cinna_credentials.py`. Never open the files yourself.

```python
from cinna_credentials import require_credential

cfg = require_credential("billing-inbox")      # by slot name
# or: require_credential("email_imap")         # by platform type

conn = imaplib.IMAP4_SSL(cfg["host"], cfg["port"]) if cfg["is_ssl"] else \
       imaplib.IMAP4(cfg["host"], cfg["port"])
conn.login(cfg["login"], cfg["password"])
```

`get_credential()` returns `None` when the slot is unconfigured;
`require_credential()` raises with a message that names the slot and where to fix it
— and never contains a value.

**Why the shim exists.** In the cloud the platform injects
`credentials/credentials.json` and there is no `.env`. The shim prefers that file
when it exists and falls back to `.env` locally, so the same call site works in both
runtimes. That is the whole portability story: change nothing at import time.

Value types are normalised for you: `true`/`false`/`yes`/`no` become booleans,
`port` becomes an int, everything else stays a string. A real environment variable
of the same name overrides the file, so a one-off run needs no edit.

## 5. Document the slot

Add a subsection to `credentials/README.md`: what the credential is for, which
scopes or permissions it needs, and a table of field → variable name. **Redacted
only** — never a value, not even a partial one.

## Cloud mapping

At import time each declared slot becomes a platform credential *draft*: the CLI
creates it by name and type and prints a setup URL. The user opens that URL and
enters the secret in the browser. **Secrets never travel through the CLI, the
manifest or the workspace copy.** See `11-go-cloud.md`.

If the user already has a credential of that name, it is shared with the agent
instead of being recreated.

## Done when

- Every external system the agent touches has a slot in `cinna-agent.json` with a
  real platform `type`, an `env_prefix` and a `fields` list.
- `credentials/.env.example` lists every variable name, with empty values.
- `credentials/README.md` describes every slot, redacted.
- `git check-ignore credentials/.env` matches, and `git status` never shows it.
- Every script gets its secrets via `cinna_credentials.py`; `grep -rn "\.env" scripts/`
  finds nothing but that module.
- No credential value appears anywhere in the conversation, the repo or `STATUS.md`.
