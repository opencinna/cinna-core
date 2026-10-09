# Team library tests

Covers `/api/v1/library` (REST) and `/git/<repo_id>.git/...` (git smart HTTP,
pre-receive policy, post-push bookkeeping and events).

## Real git, honestly

`library_git_http_test.py` and `library_push_policy_test.py` run a real `git`
subprocess. `tests/utils/git_http_bridge.py` serves a loopback port and forwards
every request (raw/chunked body, headers) to the `TestClient`, so the app runs
in-process against the test DB. Git env comes from `GitClient`
(`tests/utils/library.py`): token via `http.extraHeader` in
`GIT_CONFIG_COUNT/KEY/VALUE`, `GIT_TERMINAL_PROMPT=0`, empty `credential.helper`,
no global/system config -- nothing touches a keychain.

## Isolation seam (documented)

The git route's authorize step and `LibraryEvents._record_push` open their own
`Session(engine)` by design. The domain `conftest.py` patches those two `Session`
names to a non-closing proxy of the test session (`autouse`), plus a temp
`GIT_STORAGE_DIR`. A scratch DB was not needed: the bridge serialises requests so
one connection is safe, and the whole path stays under savepoint rollback.

## Files

- `library_api_test.py` -- create/list/get/grants/admin/delete/`cloud_install_id`.
- `library_git_http_test.py` -- round trip (v0+v2), non-ff/delete, auth + token
  kinds, grants matrix, grants over real git, gzip body.
- `library_push_policy_test.py` -- one push per refusal sentence.
- `library_events_test.py` -- `library.updated` / `library.granted` rooms, audit.

`bind_cloud_install_seam` inserts an `AgentGitSource` row directly (Rule-1 exemption).
