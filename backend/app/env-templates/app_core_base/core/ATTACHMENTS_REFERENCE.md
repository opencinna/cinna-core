# Attaching Files to Your Reply — Full Reference

You can attach a file you created to your reply message. The platform pulls the file
out of your workspace, stores it durably, and renders it as a clickable attachment
card inline in the chat (with click-to-preview and download). Attachments also travel
over A2A to native clients (Cinna Desktop / Mobile) as a real file part.

This is the reverse of a user uploading a file *to* you — here you attach a file
*to* the user.

## How to attach a file

Embed a `<cinna_attach>` tag anywhere in your reply text. The tag body is the
**absolute container path** of an existing file you created, always rooted at
`/app/workspace`:

```
<cinna_attach>/app/workspace/files/report.pdf</cinna_attach>
```

The tag (and its content) is automatically stripped from the visible chat message —
the user only sees your regular response text plus an attachment card where the tag
was. The tag's position in your text decides where the card appears inline.

### Multiple attachments

Repeat the tag, one per file. Order is preserved:

```
Here is the quarterly report and the raw data behind it.
<cinna_attach>/app/workspace/files/q4-report.pdf</cinna_attach>
<cinna_attach>/app/workspace/app-data/storage/q4-data.csv</cinna_attach>
```

### Any folder under the workspace root

The file may live anywhere under `/app/workspace`, not just `files/`:

```
<cinna_attach>/app/workspace/files/chart.png</cinna_attach>
<cinna_attach>/app/workspace/app-data/storage/export.xlsx</cinna_attach>
<cinna_attach>/app/workspace/logs/run.txt</cinna_attach>
```

Always use the **full absolute path** so files in different folders are unambiguous.

## Rules and constraints

- **Absolute path only.** The body must start with `/app/workspace`. Relative paths,
  paths outside the workspace root, and `..`/symlink escapes are rejected.
- **No name or description.** There is no caption field. The display name shown to the
  user is the file's own on-disk name (the path basename).
- **The file must already exist.** Write the file first, then emit the tag. Tags are
  processed when your reply finalises.
- **Allowed file types only.** Text (plain, markdown, csv, html, json, xml, source
  code, …), PDF, common images (png, jpeg, gif, webp), archives (zip, tar, gzip),
  Microsoft Office / OpenDocument formats, and RTF. The type is detected server-side.
- **Size limits.** Max 100MB per file; max 10 attachments per message; max 100MB total
  per message.

## What happens to rejected attachments

If a tag references a missing file, a path outside the workspace, a disallowed type,
an oversized file, or your storage quota is exceeded, that attachment is **skipped**
and the user sees a small "an attachment could not be delivered" notice instead of a
card. Your reply still completes normally — a bad attachment never fails the message.

## Notes

- Attachments are attributed to the session owner's storage.
- The same path attached twice in one message is stored once; both cards reference the
  same file.
- This convention is intentionally identical in spirit to `<webapp_action>` (see
  `webapp-framework/ACTIONS_REFERENCE.md`): a text tag you emit, post-processed by the
  platform into structured message data.

---

## Inbound file attachments over A2A

The outbound `<cinna_attach>` tag (documented above) is the agent side of the
attachment story. The inbound side — A2A clients sending files *to* the agent — uses a
separate, complementary mechanism.

### How A2A clients reference files

A2A clients attach previously-uploaded files via the vendor-namespaced key
`cinna_file_ids` on the A2A `message.metadata` object:

```json
{
  "role": "user",
  "parts": [{ "kind": "text", "text": "Please process these files." }],
  "metadata": {
    "cinna_file_ids": [
      "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    ]
  }
}
```

The value must be a **JSON list of UUID strings**, each identifying a file that was
previously uploaded to the platform. This applies to both `message/send` and
`message/stream` requests.

### Preconditions on the referenced files

Each file id must satisfy all three conditions or the whole message is rejected
downstream:

| Condition | Failure code |
|-----------|-------------|
| File exists in the platform | 400 |
| File is owned by the session user | 403 |
| File is in `temporary` status (not yet attached to another message) | 400 |

### What happens to the files

The platform parses the ids, threads them through the standard session pipeline
(`ingest_inbound_message` for `message/send`, `send_session_message` for
`message/stream`), and then attaches and materializes them into the agent
environment's upload folder — exactly like the web message API's `file_ids`
parameter. From the agent's perspective the files simply appear in its workspace,
indistinguishable from files uploaded via the UI.

### Error handling divergence from the web API

Malformed list entries (non-UUID strings, nulls, wrong types) are **silently skipped**
and logged server-side. The message still proceeds with the remaining valid ids.
This is an intentional divergence from the web API, which returns a hard 422 on any
malformed UUID.

Unknown file ids, unauthorized files, or files already in a non-temporary status
fail the **entire message** with a 400 or 403 — there is no partial delivery.

### Relationship to outbound attachments

This is the inbound counterpart to the outbound mechanism described above. Outbound
agent → user attachments travel over A2A as `FilePart` / `FileWithUri` carrying
`cinna.file_*` metadata. Inbound client → agent files travel via `cinna_file_ids`
on `message.metadata` — A2A native `FilePart` in the inbound direction is not used.

---

## Inbound file attachments over a Server Channel

Files a person attaches to a message on a **Server Channel** (a Google Chat
attachment, an email's MIME attachment part) reach you by the **identical
mechanism** as any other inbound upload: they are materialized into your
workspace and their paths are prepended to the message body as an
`Uploaded files:` block, one path per line — relative to `/app/workspace`,
your working directory — before the sender's text:

```
Uploaded files:
- ./app-data/uploads/report.pdf
---

Please summarize this.
```

There is nothing channel-specific to handle. Whether the message arrived
through the web UI, A2A, Google Chat, or email, you see the same block and the
same kind of workspace-relative path — just read the file at the path given.
