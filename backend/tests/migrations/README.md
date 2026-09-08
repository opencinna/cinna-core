# Migration tests

Tests that drive Alembic directly against a **scratch database of their own**,
created and dropped by the module that uses it.

## Why this group is not `tests/api`

`tests/README.md` requires integration tests to go through HTTP, and that rule
is right for everything with an API behind it. A migration has none. It runs
once, against rows that no endpoint can create any more — the columns it reads
are the columns it drops — and the way it fails is by quietly dropping data
rather than by returning a status code. So these tests seed raw rows and call
`alembic.command.upgrade` / `downgrade` themselves.

## Rules for this directory

- **Never touch `app_test`.** The session fixture has already migrated it to
  head; downgrading it under a running suite corrupts every other test. Create
  a scratch database, name it after the revision under test, drop it in the
  fixture teardown.
- **Reset the revision between tests.** A test that leaves its database at the
  new revision makes the next one's `upgrade` a no-op that passes without
  running a line of the migration.
- **Assert on data, not on "it did not raise".** A migration that silently
  skips a row also does not raise.
- One file per revision, named after what the revision does.

## Running

```bash
docker compose exec backend python -m pytest tests/migrations -v
```
