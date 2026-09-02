# Credentials — {{name}}

**No secret value is ever written in this file.** This is the redacted description
of what the agent needs and what each value is for — the same role the platform's
generated `credentials/README.md` plays in the cloud.

Actual values live in `credentials/.env`, which is git-ignored and never copied,
exported or pushed anywhere. Scripts read them through
`scripts/cinna_credentials.py`. Never read `.env` in a conversation, and never echo
a value back to the user.

## Required credentials

<!-- One subsection per slot declared in cinna-agent.json's `credentials[]`.
     Keep the two in sync: the slot name, type and env_prefix must match exactly. -->

<!--
### `billing-inbox` — type `email_imap`

IMAP access to the mailbox the agent reads. Read-only use.

| Field | Variable | Notes |
|-------|----------|-------|
| host | `BILLING_INBOX_HOST` | e.g. imap.example.org |
| port | `BILLING_INBOX_PORT` | 993 for SSL |
| login | `BILLING_INBOX_LOGIN` | full address |
| password | `BILLING_INBOX_PASSWORD` | app password, not the account password |
| is_ssl | `BILLING_INBOX_IS_SSL` | true |
-->

_None yet._

## Setup

```bash
cp credentials/.env.example credentials/.env
# edit credentials/.env
```

Check that nothing leaked before committing:

```bash
git check-ignore -v credentials/.env    # must print a matching rule
git status --short credentials/         # must never list .env
```

## In the cloud

After import, the platform injects `credentials/credentials.json` instead, and
`.env` is not copied. The same `require_credential("<name or type>")` call keeps
working — that is the whole point of the shim.
