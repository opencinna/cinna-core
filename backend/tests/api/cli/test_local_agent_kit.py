"""
Local Agent Kit — the public, unauthenticated ``/agent-start`` surface.

Everything here is exercised **without credentials**, which is the point: the
surface exists so a coding assistant belonging to someone who has no account can
read it. The tests therefore assert the two properties that matter for an
anonymous surface — that it serves the right static content on both mounts, and
that it cannot be talked into serving anything else (traversal, host reflection,
unbounded request volume, or anything at all on an instance that opted out).

Notes
-----
* ``kit.py`` itself (the shipped tool) is unit-tested in
  ``tests/unit/test_local_kit_tool.py``; this file covers the serving side only.
* The kit has a second representation, the **contract** — the machine-readable
  subset (``kit.json``, ``layout.json``, ``CONTRACT_VERSION``, ``CHANGELOG.md``,
  ``schema/``, ``templates/``) a non-kit host consumes at ``/contract.tar.gz``
  and ``/contract/version``. It is a subset of the same rendered tree, never a
  second tree, and it inherits every piece of this surface's plumbing.
* The rendered tree and tarball are memoized process-wide on a snapshot mtime
  key. Two autouse fixtures reset the per-process state that key does not cover:
  the rate limiter (otherwise the 429 boundary would depend on how many requests
  earlier tests happened to make) and the build memo (otherwise a test that
  monkeypatches a setting would read the previous test's render, since patching
  a setting does not move the snapshot's mtime).
"""

from __future__ import annotations

import ast
import io
import json
import re
import tarfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.cli.local_agent_kit_service import (
    CONTRACT_MEMBERS,
    CONTRACT_MEMBER_PREFIXES,
    CONTRACT_TARBALL_FILENAME,
    CONTRACT_TARBALL_ROOT,
    instance_display_name,
)

# Both mounts of the same router. Every content assertion runs against both:
# `/agent-start` is the pasteable URL, `/api/agent-start` the alias that survives a reverse
# proxy nobody updated, and a kit that differs between them is a broken kit.
MOUNTS = ["/agent-start", "/api/agent-start"]

# The two contract representations, relative to a mount. Both inherit the kit
# surface's plumbing without exception (D9): the enabled guard, the rate limit,
# per-representation ETags, `X-Kit-Version`, `Cache-Control`.
CONTRACT_PATHS = ["/contract.tar.gz", "/contract/version"]

# ── Token provenance ─────────────────────────────────────────────────────
# Two classes of `{{TOKEN}}` travel in this tree and **neither their shape nor
# their membership tells them apart**: both are `{{UPPER_SNAKE}}`, and
# `KIT_VERSION` is in both sets. What settles it is PROVENANCE — who
# substitutes the token, and when:
#
#   platform  `LocalAgentKitService.placeholders()` plus `{{KIT_VERSION}}`,
#             substituted here before download, so none of them may survive
#             into a served file;
#   scaffold  `tools/kit.py`'s `SCAFFOLD_TOKENS`, filled by `kit.py new` on the
#             user's machine, so the ones the platform does *not* also render
#             must survive untouched.
#
# `KIT_VERSION` sits in both on purpose — the platform fills it when it renders
# a kit for download, `kit.py new` fills it when the kit was never rendered —
# and for a *served* tree the platform's provenance is the one that applies.
# `kit.py`'s `SCAFFOLD_TOKENS` comment is the upstream authority for all of this
# and says so in as many words; the helpers below derive both sets from the
# artefacts that own them rather than restating either. That is the whole point:
# the previous scan classified by CASE, and when the scaffold tokens moved from
# `{{name}}` to `{{NAME}}` it did not go red — it silently began flagging every
# legitimate placeholder as a leftover. A flat list would have failed the same
# way one change later.
_ANY_TOKEN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}")


def _platform_token_names() -> frozenset[str]:
    """Every token the *platform* substitutes, from the renderer itself."""
    from app.services.cli.local_agent_kit_service import (
        LocalAgentKitService,
        _VERSION_TOKEN,
    )

    # `KIT_VERSION` is resolved after hashing, so it is deliberately absent from
    # `placeholders()` — it is still platform-rendered and still must not ship.
    version_token = _ANY_TOKEN.fullmatch(_VERSION_TOKEN)
    assert version_token is not None, _VERSION_TOKEN
    return frozenset(LocalAgentKitService.placeholders()) | {version_token.group(1)}


def _scaffold_token_names(members: dict[str, bytes]) -> frozenset[str]:
    """`SCAFFOLD_TOKENS` as the *shipped* `tools/kit.py` declares it.

    Parsed rather than imported: `kit.py` is a stdlib-only script that runs on
    the user's machine, and the copy that matters is the one this response just
    served — the same bytes the scan runs over.
    """
    for node in ast.parse(members["tools/kit.py"].decode("utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SCAFFOLD_TOKENS"
            for target in node.targets
        ):
            return frozenset(ast.literal_eval(node.value))
    pytest.fail(
        "tools/kit.py no longer declares a module-level SCAFFOLD_TOKENS tuple. "
        "The placeholder scan classifies tokens by provenance and reads that "
        "declaration for the scaffold half — re-point it at wherever the "
        "declaration moved; do not fall back to matching on case."
    )


@pytest.fixture(autouse=True)
def _fresh_kit_rate_limiter(monkeypatch):
    """Give each test its own limiter bucket.

    The router's limiter is module-global and process-local, so without this the
    number of requests an earlier test made would move the 429 boundary this
    file asserts.
    """
    from app.api.routes import local_agent_kit
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(local_agent_kit, "_kit_rate_limiter", RateLimiter())


@pytest.fixture(autouse=True)
def _fresh_kit_build(monkeypatch):
    """Drop the process-wide rendered-kit memo before each test.

    The build is keyed on the snapshot's mtime + file count, which is right in
    production (settings are deploy-time constants and a redeploy moves the
    mtimes) but wrong in a test that monkeypatches a setting: the key does not
    move, so the next assertion reads the *previous* test's render. Clearing it
    here means no test has to remember, and no future one fails confusingly.
    """
    from app.services.cli.local_agent_kit_service import LocalAgentKitService

    monkeypatch.setattr(LocalAgentKitService, "_cache", None)


def _tarball_members(client: TestClient, mount: str = "/api/agent-start") -> dict[str, bytes]:
    """Download and unpack the kit tarball into ``{relative path: bytes}``."""
    response = client.get(f"{mount}/kit.tar.gz")
    assert response.status_code == 200, response.text
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        for info in tar.getmembers():
            assert info.name.startswith("cinna-kit/"), info.name
            extracted = tar.extractfile(info)
            assert extracted is not None
            members[info.name[len("cinna-kit/") :]] = extracted.read()
    return members


def _contract_tarball_members(
    client: TestClient, mount: str = "/api/agent-start"
) -> dict[str, bytes]:
    """Download and unpack the *contract* tarball into ``{relative path: bytes}``.

    Deliberately a second helper rather than a ``root=`` parameter on the one
    above: the root prefix is itself under test on this side (D9 — exactly one
    top-level ``cinna-contract/``), and a shared helper parameterised on it
    would assert that property inside the helper instead of in the test that
    owns it.
    """
    response = client.get(f"{mount}/contract.tar.gz")
    assert response.status_code == 200, response.text
    prefix = f"{CONTRACT_TARBALL_ROOT}/"
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        for info in tar.getmembers():
            assert info.name.startswith(prefix), info.name
            extracted = tar.extractfile(info)
            assert extracted is not None
            members[info.name[len(prefix) :]] = extracted.read()
    return members


# ---------------------------------------------------------------------------
# Both mounts, content negotiation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mount", MOUNTS)
@pytest.mark.parametrize("path", ["", "/"])
def test_mount_root_serves_markdown_without_auth(
    client: TestClient, mount: str, path: str
) -> None:
    """`/agent-start`, `/agent-start/`, and both under `/api/`, all serve START.md.

    The trailing-slash spelling matters: an assistant piping `curl` at a URL a
    human typed must not need to know which one the router prefers.
    """
    response = client.get(f"{mount}{path}")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert "cinna-kit" in response.text


def test_both_mounts_serve_identical_content(client: TestClient) -> None:
    """The alias is the same router, not a second copy that can drift."""
    canonical = client.get("/agent-start")
    alias = client.get("/api/agent-start")

    assert canonical.status_code == alias.status_code == 200
    assert canonical.text == alias.text
    assert canonical.headers["x-kit-version"] == alias.headers["x-kit-version"]


def test_browser_accept_header_gets_the_html_landing(client: TestClient) -> None:
    """A browser ranks text/html first, so it gets a readable page."""
    response = client.get(
        "/agent-start",
        headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>" in response.text
    # The CSP is set on the HTML response only — it is the one response that
    # executes anything (a copy button).
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    )


def test_wildcard_accept_gets_markdown(client: TestClient) -> None:
    """`curl` and most assistants send `*/*` and must not get HTML."""
    response = client.get("/agent-start", headers={"Accept": "*/*"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")


@pytest.mark.parametrize(
    "format_param,expected",
    [("md", "text/markdown"), ("html", "text/html")],
)
def test_format_query_overrides_accept(
    client: TestClient, format_param: str, expected: str
) -> None:
    """`?format=` wins over `Accept` in both directions."""
    # Each request sends the Accept header that would produce the *other* result.
    accept = "text/html" if format_param == "md" else "*/*"
    response = client.get(f"/agent-start?format={format_param}", headers={"Accept": accept})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected)


def test_unknown_format_value_falls_back_to_markdown(client: TestClient) -> None:
    """A typo'd `?format=` must not 422 the entrypoint.

    Every spelling of this URL has to yield the instructions; a validation error
    would leave the assistant with nothing to act on and no obvious next step.
    """
    response = client.get("/agent-start?format=markdown")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")


def test_html_landing_embeds_the_full_markdown(client: TestClient) -> None:
    """A mis-negotiated Accept must never hide instructions.

    Both variants carry the complete START.md, so an HTML→text converter still
    yields a usable document.
    """
    markdown = client.get("/agent-start?format=md").text
    landing = client.get("/agent-start?format=html").text

    # Compare on a distinctive line, escaped the way the landing embeds it.
    import html as html_module

    for line in markdown.splitlines():
        if len(line) > 40 and "<" not in line and "&" not in line:
            assert html_module.escape(line) in landing
            break
    else:  # pragma: no cover - START.md always has a long plain line
        pytest.fail("START.md had no line long enough to sample")


@pytest.mark.parametrize("mount", MOUNTS)
def test_start_md_path_is_always_raw_markdown(client: TestClient, mount: str) -> None:
    """`/START.md` ignores Accept — it is the raw file by name."""
    response = client.get(f"{mount}/START.md", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text == client.get(f"{mount}?format=md").text


# ---------------------------------------------------------------------------
# Version: endpoint == tarball VERSION == header
# ---------------------------------------------------------------------------


def test_version_endpoint_matches_tarball_and_header(client: TestClient) -> None:
    """One version, three carriers.

    `kit.py refresh` compares the extracted `VERSION` file with `/version`; if
    those two can disagree, every kit on every machine is either permanently
    stale or permanently churning.
    """
    version_response = client.get("/api/agent-start/version")
    assert version_response.status_code == 200
    payload = version_response.json()

    members = _tarball_members(client)
    tarball_version = members["VERSION"].decode("utf-8").strip()

    assert payload["kit_version"] == tarball_version
    assert version_response.headers["x-kit-version"] == tarball_version
    assert client.get("/agent-start").headers["x-kit-version"] == tarball_version
    # kit.json carries it too, so an assistant that only read the index knows.
    assert json.loads(members["kit.json"])["kit_version"] == tarball_version


def test_version_is_a_content_hash_not_a_placeholder(client: TestClient) -> None:
    """The version must be substituted, not shipped as its own token."""
    version = client.get("/api/agent-start/version").json()["kit_version"]

    assert re.fullmatch(r"[0-9a-f]{16}", version), version


# ---------------------------------------------------------------------------
# Kit files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mount", MOUNTS)
def test_kit_file_fetch(client: TestClient, mount: str) -> None:
    """Individual files are fetchable for an assistant that cannot untar."""
    response = client.get(f"{mount}/kit/guides/11-go-cloud.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert len(response.content) > 0


def test_dotfile_under_templates_is_served(client: TestClient) -> None:
    """A scaffold is broken without its dotfiles.

    `templates/agent/credentials/.gitignore` is what keeps `credentials/.env`
    out of the user's git history, so it has to survive both the sync and the
    render.
    """
    response = client.get("/api/agent-start/kit/templates/agent/credentials/.gitignore")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert ".env" in response.text


@pytest.mark.parametrize(
    "member",
    [
        "templates/agent/gitignore",
        "templates/root/gitignore",
        "templates/agent/app-data/cache/gitignore",
        "templates/agent/.claude/settings.local.json",
        "templates/agent/credentials/.gitignore",
        "templates/agent/app-data/storage/.gitkeep",
        "templates/agent/app-data/uploads/.gitkeep",
    ],
)
def test_scaffold_dotfiles_survive_the_sync(client: TestClient, member: str) -> None:
    """The scaffold's own ignore rules are content, and git does not know that.

    A dotted `templates/agent/.gitignore` naming `app-data/` would apply to
    *this repository* as much as to the user's machine, hiding the three
    `app-data/` placeholders from `git add`; the same happens to
    `.claude/settings.local.json` under a global excludes file. Nothing else
    would notice: the sync copies whatever is on disk, so a clone missing them
    yields a smaller kit, a different `kit_version`, and a scaffold whose
    runtime output directories do not exist — silently, and only for everyone
    who was not the author.

    The structural fix is the dotless `gitignore` name (restored to
    `.gitignore` by `kit.py new`) plus the negations in the repository's root
    `.gitignore` for the two `.claude/` copies. This test is the tripwire: it
    fails on any checkout where a file was not committed, which is the only
    moment the omission is still cheap to fix.
    """
    assert client.get(f"/api/agent-start/kit/{member}").status_code == 200


def test_kit_json_is_served_at_both_spellings(client: TestClient) -> None:
    """`/kit.json` and `/kit/kit.json` are the same bytes."""
    direct = client.get("/api/agent-start/kit.json")
    via_tree = client.get("/api/agent-start/kit/kit.json")

    assert direct.status_code == via_tree.status_code == 200
    assert direct.content == via_tree.content
    assert direct.headers["content-type"].startswith("application/json")


def test_tarball_is_rooted_and_has_the_download_headers(client: TestClient) -> None:
    """Extracting anywhere yields one obvious folder, not a scattered mess."""
    response = client.get("/api/agent-start/kit.tar.gz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/tar+gzip"
    assert 'filename="cinna-kit.tar.gz"' in response.headers["content-disposition"]

    members = _tarball_members(client)
    assert "START.md" in members
    assert "kit.json" in members
    assert "tools/kit.py" in members
    assert "templates/agent/gitignore" in members


def test_tarball_bytes_are_deterministic(client: TestClient) -> None:
    """Two builds of the same kit must be byte-identical.

    A wall-clock timestamp in the members or the gzip header would make two
    workers serve different bodies under one strong ETag — exactly what a
    validator promises cannot happen.
    """
    from app.services.cli.local_agent_kit_service import LocalAgentKitService

    first = client.get("/api/agent-start/kit.tar.gz").content
    LocalAgentKitService._cache = None  # force a genuine rebuild
    second = client.get("/api/agent-start/kit.tar.gz").content

    assert first == second


def test_served_file_matches_the_tarball_copy(client: TestClient) -> None:
    """One rendered tree feeds both transports — they cannot diverge."""
    members = _tarball_members(client)
    served = client.get("/api/agent-start/kit/tools/kit.py")

    assert served.status_code == 200
    assert served.content == members["tools/kit.py"]


# ---------------------------------------------------------------------------
# The contract — the machine-readable subset a non-kit host consumes (D9, D16)
#
# `/contract.tar.gz` and `/contract/version` are a second *representation* of
# the same rendered tree, not a second tree. Two trees would be two truths, and
# the drift would only surface when a Cinna Desktop scaffold and a `kit.py new`
# scaffold stopped matching — on someone else's machine, months later.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", CONTRACT_PATHS)
def test_contract_is_served_on_both_mounts(client: TestClient, path: str) -> None:
    """The alias carries the contract too, byte-for-byte.

    A host that pulls the contract through `/api/agent-start` because its proxy
    has no pretty `/agent-start` location must get the same archive, not a
    second copy that can drift.
    """
    canonical = client.get(f"/agent-start{path}")
    alias = client.get(f"/api/agent-start{path}")

    assert canonical.status_code == alias.status_code == 200, canonical.text
    assert canonical.content == alias.content
    assert canonical.headers["x-kit-version"] == alias.headers["x-kit-version"]
    assert canonical.headers["etag"] == alias.headers["etag"]


def test_contract_tarball_has_exactly_one_top_level_directory(
    client: TestClient,
) -> None:
    """Extracting anywhere yields one obvious folder, `cinna-contract/`.

    The consuming host descends exactly one level (D9). A second top-level
    entry — or a member packed at the root — makes that descent land somewhere
    unintended, and the desktop's `isContractTree` probe then fails against a
    tree that is actually complete.
    """
    response = client.get("/api/agent-start/contract.tar.gz")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/tar+gzip"
    assert (
        f'filename="{CONTRACT_TARBALL_FILENAME}"'
        in response.headers["content-disposition"]
    )

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        names = [info.name for info in tar.getmembers()]

    assert names, "the contract archive is empty"
    assert {name.split("/")[0] for name in names} == {CONTRACT_TARBALL_ROOT}
    # Every member is *below* the root, not the root itself: a bare
    # `cinna-contract` entry would pass the set check above and still be a file
    # sitting where the directory should be.
    assert all(len(name.split("/")) > 1 for name in names), names


def test_contract_membership_is_the_declared_subset(client: TestClient) -> None:
    """What the contract carries, and — the load-bearing half — what it does not.

    Spelled out as literals on purpose. This is the independent statement of
    intent; `test_every_declared_contract_member_reaches_the_tree` reads the
    service's own declaration, so between them a change to `CONTRACT_MEMBERS`
    has to be made deliberately in two places rather than sliding through.

    The exclusions each have a reason worth keeping:

    * `VERSION` holds the *kit* content version. Shipping it inside a contract
      tree would put two meanings on one filename, next to the `CONTRACT_VERSION`
      that is the contract's own.
    * `START.md`, `README.md`, `guides/`, `assistants/` and `tools/` are prose
      for a human or a coding assistant. `assistants/` in particular is
      deliberately outside the contract even though it now carries a
      desktop-facing page — the contract is the machine-readable half.
    """
    members = _contract_tarball_members(client)

    for member in ("kit.json", "layout.json", "CONTRACT_VERSION", "CHANGELOG.md"):
        assert member in members, sorted(members)
    for prefix in ("schema/", "templates/"):
        assert any(rel.startswith(prefix) for rel in members), (prefix, sorted(members))

    for member in ("VERSION", "START.md", "README.md"):
        assert member not in members, member
    for prefix in ("guides/", "assistants/", "tools/"):
        assert not [rel for rel in members if rel.startswith(prefix)], prefix


def test_every_declared_contract_member_reaches_the_tree(client: TestClient) -> None:
    """A declared member that is not in the tree ships as a silent 200.

    The packer is a **filter**, not an assertion: a member that is not in the
    rendered tree is simply not packed, so a rename of `templates/` in the
    snapshot yields a perfectly valid archive, a perfectly valid ETag, and no
    templates. The service's own coherence gate (D16) covers only the identity
    trio — `kit.json`, `layout.json`, `CONTRACT_VERSION` — because a thin
    `schema/` or `templates/` is a *content* problem and must not be dressed up
    as an identity failure. That is precisely why the completeness assertion has
    to live somewhere, and this is the somewhere.

    Reads the declaration rather than restating it, so a member added to
    `CONTRACT_MEMBERS` is covered the day it is added.
    """
    members = _contract_tarball_members(client)

    assert sorted(m for m in CONTRACT_MEMBERS if m not in members) == []
    assert (
        sorted(
            prefix
            for prefix in CONTRACT_MEMBER_PREFIXES
            if not any(rel.startswith(prefix) for rel in members)
        )
        == []
    )


def test_contract_tarball_bytes_are_deterministic(client: TestClient) -> None:
    """Two builds of the same contract must be byte-identical.

    Same property as the kit tarball, asserted separately because it is a
    separate archive: a wall-clock timestamp in the members or the gzip header
    would make two workers serve different bodies under one strong ETag.
    """
    from app.services.cli.local_agent_kit_service import LocalAgentKitService

    first = client.get("/api/agent-start/contract.tar.gz").content
    LocalAgentKitService._cache = None  # force a genuine rebuild
    second = client.get("/api/agent-start/contract.tar.gz").content

    assert first
    assert first == second


def test_contract_version_payload_carries_both_versions(client: TestClient) -> None:
    """`/contract/version` publishes the number the archive actually carries.

    The two versions answer different questions and must not be confused:
    `contract_version` is a hand-maintained semver the consuming host's
    major-version compatibility gate keys on, `kit_version` is a content hash
    that moves on any edit. Publishing a `contract_version` that disagrees with
    the `CONTRACT_VERSION` member is worse than publishing nothing, because a
    false *compatibility* fails silently where a refusal at least stops.
    """
    payload = client.get("/api/agent-start/contract/version").json()
    kit_payload = client.get("/api/agent-start/version").json()
    members = _contract_tarball_members(client)

    shipped = members["CONTRACT_VERSION"].decode("utf-8").strip()
    assert payload["contract_version"] == shipped
    assert re.fullmatch(r"\d+\.\d+\.\d+", payload["contract_version"]), shipped

    assert payload["kit_version"] == kit_payload["kit_version"]
    assert re.fullmatch(r"[0-9a-f]{16}", payload["kit_version"])
    assert payload["contract_version"] != payload["kit_version"]

    # One envelope, one payload builder: `/contract/version` is `/version` plus
    # the one key, so a host polling either parses the same shape.
    assert {k: v for k, v in payload.items() if k != "contract_version"} == kit_payload


@pytest.mark.parametrize("path", CONTRACT_PATHS)
def test_contract_responses_carry_the_kit_version_not_the_contract_version(
    client: TestClient, path: str
) -> None:
    """`X-Kit-Version` on a contract response is the *kit* version.

    `contract_version` is hand-maintained and does not move when a template or
    the schema changes, so a validator keyed on it would answer "unchanged" to a
    host that is in fact holding a stale contract — the one thing a validator
    promises cannot happen. The ETag is scoped the same way, off `kit_version`.
    """
    contract_version = client.get("/api/agent-start/contract/version").json()[
        "contract_version"
    ]
    kit_version = client.get("/api/agent-start/version").json()["kit_version"]

    response = client.get(f"/api/agent-start{path}")

    assert response.status_code == 200, response.text
    assert response.headers["x-kit-version"] == kit_version
    assert response.headers["x-kit-version"] != contract_version
    assert response.headers["etag"].startswith(f'"{kit_version}-')
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_contract_and_kit_representations_do_not_share_a_validator(
    client: TestClient,
) -> None:
    """One kit version, four resources, four validators.

    Every response carries the same `X-Kit-Version`, so the ETag folds the
    representation in. Without that, a host holding the kit tarball's validator
    would be told the contract tarball — a *different, smaller archive* — is
    unchanged, and would never fetch it.
    """
    kit_tarball = client.get("/api/agent-start/kit.tar.gz")
    contract_tarball = client.get("/api/agent-start/contract.tar.gz")
    kit_version = client.get("/api/agent-start/version")
    contract_version = client.get("/api/agent-start/contract/version")

    tags = {
        response.headers["etag"]
        for response in (kit_tarball, contract_tarball, kit_version, contract_version)
    }
    assert len(tags) == 4, tags
    assert (
        kit_tarball.headers["x-kit-version"]
        == contract_tarball.headers["x-kit-version"]
    )
    # The contract really is the smaller archive, so a shared validator would
    # have been a live bug rather than a theoretical one.
    assert contract_tarball.content != kit_tarball.content
    assert len(contract_tarball.content) < len(kit_tarball.content)

    crossed = client.get(
        "/api/agent-start/contract.tar.gz",
        headers={"If-None-Match": kit_tarball.headers["etag"]},
    )

    assert crossed.status_code == 200
    assert crossed.content == contract_tarball.content


def test_contract_shares_the_kit_rate_limit_budget(
    client: TestClient, monkeypatch
) -> None:
    """One budget per caller across the whole surface, contract included.

    The contract tarball is the most expensive thing here to hand out, so a
    per-URL budget — or a contract route that skipped the limiter dependency —
    would be no budget at all.
    """
    monkeypatch.setattr(settings, "LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN", 2)

    assert client.get("/api/agent-start/contract/version").status_code == 200
    assert client.get("/api/agent-start/contract.tar.gz").status_code == 200

    blocked = client.get("/agent-start/contract.tar.gz")

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/agent-start/kit/nope.md",
        "/api/agent-start/kit/guides/",
        "/api/agent-start/kit/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/api/agent-start/kit/..%2f..%2f..%2fetc%2fpasswd",
        "/api/agent-start/kit//etc/passwd",
        "/api/agent-start/kit/....//....//etc/passwd",
        "/api/agent-start/kit/..\\..\\etc\\passwd",
        "/api/agent-start/kit/",
        "/api/agent-start/kit",
    ],
)
def test_unknown_and_traversal_paths_are_404(client: TestClient, path: str) -> None:
    """Nothing outside the kit is addressable.

    Serving from an in-memory dict is what makes this true: a path that is not a
    key resolves to nothing at all, so there is no filesystem read to escape
    from. These cases assert the property rather than the implementation.
    """
    response = client.get(path)

    assert response.status_code == 404, f"{path} → {response.status_code}"
    assert "root:" not in response.text


def test_host_header_is_never_reflected(client: TestClient) -> None:
    """Placeholders come from settings, never from the request.

    The go-cloud guide tells the user which host to run `cinna login` against.
    Reflecting an attacker-supplied Host there would aim that login — and the
    device-flow token it mints — at the attacker.
    """
    response = client.get(
        "/api/agent-start/kit.json", headers={"Host": "evil.example"}
    )

    assert response.status_code == 200
    body = response.text
    assert "evil.example" not in body
    assert json.loads(body)["platform_url"] == settings.FRONTEND_HOST.rstrip("/")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_no_unrendered_placeholder_survives(client: TestClient) -> None:
    """A token the *platform* owns is a typo nobody would notice.

    A misspelt `{{PLATFORM_URl}}` renders as itself and reaches the user's
    machine as literal braces in an instruction. Scanning the whole rendered
    tree catches it here instead.

    The scan matches **any** `{{token}}`, in any case, and exempts exactly one
    class: the scaffold tokens `kit.py new` fills in on the user's machine,
    minus any the platform also renders. So a platform token that failed to
    substitute, a typo, a token nobody has heard of, and a reintroduced
    lowercase `{{name}}` are all leftovers. Nothing here keys on case — see the
    provenance note at the top of the file for why case cannot decide this.
    """
    members = _tarball_members(client)
    platform = _platform_token_names()
    scaffold = _scaffold_token_names(members)
    survivors = scaffold - platform

    # Both halves must be non-empty or the exemption is decoration: an empty
    # `survivors` degrades the scan to "no braces anywhere" (which the templates
    # would fail), and an empty `platform` would mean the renderer substitutes
    # nothing at all.
    assert platform, "the renderer declares no tokens; this scan has nothing to catch"
    assert survivors, (
        "every scaffold token is also platform-rendered, so this scan now "
        "exempts nothing — reconcile kit.py's SCAFFOLD_TOKENS with "
        "LocalAgentKitService.placeholders()"
    )

    leftovers: dict[str, list[str]] = {}
    for rel, content in members.items():
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            pytest.fail(f"{rel} is not UTF-8 text; the kit ships text only")
        found = sorted(set(_ANY_TOKEN.findall(text)) - survivors)
        if found:
            leftovers[rel] = found

    assert leftovers == {}


def test_scaffold_placeholders_are_left_for_kit_py(client: TestClient) -> None:
    """The scaffold's own tokens are *not* ours to render.

    They belong to `kit.py new`, which fills them in on the user's machine with
    the agent's own name. Rendering them server-side would ship a scaffold with
    the instance name baked in where the agent name belongs — so this asserts
    they survive, the complement of the test above.

    No token is spelled out. `kit.py`'s `SCAFFOLD_TOKENS` is the authority for
    which they are, and the assertion is that every one the platform does *not*
    render reaches the user intact.
    """
    members = _tarball_members(client)
    platform = _platform_token_names()
    scaffold = _scaffold_token_names(members)

    # Membership cannot classify either — `KIT_VERSION` is in both sets, and for
    # a served tree the platform's provenance is the one that wins. Pinned so a
    # later "simplification" back to a single flat list is a red test rather than
    # a silently vacuous one. If a second dual-provenance token is ever added,
    # update this alongside `kit.py`'s SCAFFOLD_TOKENS comment, which is the
    # upstream authority both this and the desktop's MANIFEST_TOKENS derive from.
    assert scaffold & platform == {"KIT_VERSION"}, (sorted(scaffold), sorted(platform))

    templates = [
        content.decode("utf-8")
        for rel, content in members.items()
        if rel.startswith("templates/")
    ]
    assert templates, "the kit shipped no templates/ member at all"

    for name in sorted(scaffold - platform):
        token = "{{" + name + "}}"
        assert any(token in text for text in templates), (
            f"{token} is declared a scaffold token but no shipped template "
            "carries it — either the templates lost it, or the declaration is "
            "stale"
        )

    # `templates/agent/AGENTS.md` is the file a reader checks by hand, so it is
    # named; the token it must carry is still derived, not spelled out.
    agent_template = members["templates/agent/AGENTS.md"].decode("utf-8")
    assert any("{{" + name + "}}" in agent_template for name in scaffold - platform)


def test_every_rendered_json_member_parses(client: TestClient) -> None:
    """Substitution must not break the JSON it is spliced into.

    Placeholder values are operator-controlled text: `PROJECT_NAME` may contain a
    quote, and a raw splice into `"instance_name": "{{INSTANCE_NAME}}"` would
    emit a file no parser accepts — silently, since nothing else reads it.
    """
    for rel, content in _tarball_members(client).items():
        if rel.endswith(".json"):
            json.loads(content)  # raises with the member name in the traceback


def test_json_survives_a_placeholder_value_containing_a_quote(
    client: TestClient, monkeypatch
) -> None:
    """The escaping is asserted against a hostile value, not a lucky one.

    Whether this instance's own ``PROJECT_NAME`` happens to contain a quote must
    not decide whether the guard has teeth, so the value is forced here. The
    rendered tree is memoized on a snapshot key (settings are deploy-time
    constants), so the cache is dropped to make the new value take effect.
    """
    from app.services.cli.local_agent_kit_service import LocalAgentKitService

    monkeypatch.setattr(settings, "PROJECT_NAME", 'Acme "Labs" \\ Ltd')
    monkeypatch.setattr(LocalAgentKitService, "_cache", None)

    index = client.get("/api/agent-start/kit.json").json()

    assert index["instance_name"] == 'Acme "Labs" \\ Ltd'


def test_placeholders_resolve_to_settings_values(client: TestClient) -> None:
    """The kit describes *this* instance, and the values have one source."""
    index = client.get("/api/agent-start/kit.json").json()

    assert index["platform_url"] == settings.FRONTEND_HOST.rstrip("/")
    assert index["kit_base_url"] == f"{settings.backend_base_url}/api/agent-start"
    assert index["start_url"] == f"{settings.FRONTEND_HOST.rstrip('/')}/agent-start"
    assert index["signup_url"] == f"{settings.FRONTEND_HOST.rstrip('/')}/signup"
    assert index["login_url"] == f"{settings.FRONTEND_HOST.rstrip('/')}/login"
    assert index["instance_name"] == instance_display_name(settings.PROJECT_NAME)
    assert index["cli"]["install_spec"] == settings.CINNA_CLI_INSTALL_SPEC
    assert index["cli"]["min_version"] == settings.MINIMUM_CLI_VERSION


def test_kit_base_url_uses_the_api_alias(client: TestClient) -> None:
    """Kit-internal links must work on an instance whose proxy has no /agent-start."""
    index = client.get("/api/agent-start/kit.json").json()

    assert index["kit_base_url"].endswith("/api/agent-start")


def test_version_payload_matches_the_index(client: TestClient) -> None:
    """`/version` is what `kit.py refresh` reads; it must agree with kit.json."""
    payload = client.get("/api/agent-start/version").json()
    index = client.get("/api/agent-start/kit.json").json()

    assert payload["kit_version"] == index["kit_version"]
    assert payload["kit_base_url"] == index["kit_base_url"]
    assert payload["cli"] == index["cli"]

    # D17 dissolved the `schema_version` coupling this used to assert: it was a
    # synthesised constant that decided nothing, and it left `_version_payload`
    # and `kit.json` in one change. The assertion becomes the opposite one
    # rather than being dropped, because the two sites must move *together* —
    # a wire shape carrying a field with no backing artefact in the tree (or the
    # reverse) reads as a serving bug and sends the next reader hunting a fault
    # that is not there. The compatibility gate is `contract_version`, on
    # `/contract/version`. The **manifest's** `schema_version` (in
    # `cinna-agent.json`) shares only the name: it is a live, load-bearing
    # legacy marker and is untouched by this.
    assert "schema_version" not in payload
    assert "schema_version" not in index


# ---------------------------------------------------------------------------
# Caching headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/agent-start", "/api/agent-start/START.md", "/api/agent-start/kit.json", "/api/agent-start/version"],
)
def test_responses_carry_cache_and_hardening_headers(
    client: TestClient, path: str
) -> None:
    """Identical for every caller, so intermediaries may cache them."""
    response = client.get(path)

    assert response.status_code == 200
    version = response.headers["x-kit-version"]
    # The validator is scoped to the representation; the kit-wide content
    # version rides its own header (see test_etag_is_per_representation).
    assert response.headers["etag"].startswith(f'"{version}-')
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Origin-less callers (curl, kit.py) see the route's own value; a
    # cross-origin fetch is covered by test_kit_version_is_readable_cross_origin,
    # where the app-wide CORS middleware replaces it with the echoed origin.
    assert response.headers["access-control-allow-origin"] == "*"


def test_etag_is_per_representation_not_per_kit(client: TestClient) -> None:
    """One kit version, but a validator that answers for one resource.

    Every response carries the same `X-Kit-Version`, so reusing it as the ETag
    would make an `If-None-Match` from *any* URL match *every* other URL — a
    client that carries one validator across fetches would be told a file it has
    never seen is unchanged.
    """
    first = client.get("/api/agent-start/kit/guides/01-first-agent.md")
    second = client.get("/api/agent-start/kit/guides/11-go-cloud.md")

    assert first.headers["x-kit-version"] == second.headers["x-kit-version"]
    assert first.headers["etag"] != second.headers["etag"]

    crossed = client.get(
        "/api/agent-start/kit/guides/11-go-cloud.md",
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert crossed.status_code == 200
    assert crossed.content == second.content


def test_markdown_and_html_variants_do_not_share_a_validator(
    client: TestClient,
) -> None:
    """Two bodies at one URL under `Cache-Control: public`.

    Without distinct ETags an intermediary could revalidate the markdown with
    the HTML page's tag and serve a browser page to a `curl`-piping assistant.
    """
    markdown = client.get("/agent-start?format=md")
    landing = client.get("/agent-start?format=html")

    assert markdown.headers["etag"] != landing.headers["etag"]

    crossed = client.get(
        "/agent-start?format=md", headers={"If-None-Match": landing.headers["etag"]}
    )

    assert crossed.status_code == 200


def test_content_negotiated_root_varies_on_accept(client: TestClient) -> None:
    """A public cache must key the mount root on Accept, or it will mix them up."""
    response = client.get("/agent-start")

    vary = {v.strip().lower() for v in response.headers["vary"].split(",")}
    assert "accept" in vary


def test_kit_version_is_readable_cross_origin(client: TestClient) -> None:
    """A browser-hosted assistant must be able to read the version header.

    The app-wide CORS middleware *replaces* whatever a route sets for
    `Access-Control-Expose-Headers`, so listing the header on the response alone
    is not enough — it has to be in the middleware's allowlist, and a
    cross-origin fetch is the only thing that proves it is.
    """
    response = client.get(
        "/api/agent-start/version", headers={"Origin": "https://assistant.example"}
    )

    assert response.status_code == 200
    exposed = {
        h.strip().lower()
        for h in response.headers["access-control-expose-headers"].split(",")
    }
    assert "x-kit-version" in exposed


@pytest.mark.parametrize(
    "path",
    [
        "/agent-start",
        "/api/agent-start",
        "/api/agent-start/START.md",
        "/api/agent-start/version",
        "/api/agent-start/kit.json",
        "/api/agent-start/kit.tar.gz",
        "/api/agent-start/kit/guides/11-go-cloud.md",
        # The contract inherits this plumbing rather than forking it (D9).
        "/api/agent-start/contract.tar.gz",
        "/api/agent-start/contract/version",
    ],
)
def test_if_none_match_returns_304(client: TestClient, path: str) -> None:
    """A returning assistant re-checks cheaply instead of re-downloading."""
    first = client.get(path)
    assert first.status_code == 200
    etag = first.headers["etag"]

    second = client.get(path, headers={"If-None-Match": etag})

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["x-kit-version"] == first.headers["x-kit-version"]


def test_stale_etag_still_serves_the_body(client: TestClient) -> None:
    """Only a matching version short-circuits; anything else gets content."""
    response = client.get("/agent-start", headers={"If-None-Match": '"0000000000000000"'})

    assert response.status_code == 200
    assert len(response.content) > 0


def test_if_none_match_on_unknown_path_is_404_not_304(client: TestClient) -> None:
    """A stale ETag must not turn a missing file into "unchanged"."""
    etag = client.get("/agent-start").headers["etag"]

    response = client.get(
        "/api/agent-start/kit/does-not-exist.md", headers={"If-None-Match": etag}
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limited_per_client_ip(client: TestClient, monkeypatch) -> None:
    """An anonymous surface needs a backstop against tarball hammering.

    Asserted through the route (429 + Retry-After), not by poking the limiter.
    """
    monkeypatch.setattr(settings, "LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN", 3)

    for _ in range(3):
        assert client.get("/api/agent-start/version").status_code == 200

    blocked = client.get("/api/agent-start/version")

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_spoofed_forwarded_for_cannot_buy_a_fresh_budget(
    client: TestClient, monkeypatch
) -> None:
    """The limiter key must not be a header the caller writes.

    `X-Forwarded-For` is caller input unless our own proxy appended it. If the
    limiter trusted the first hop, a caller sending a new value per request
    would have no limit at all — and, worse, could mint enough distinct keys to
    fill the limiter's key ceiling and push every real visitor into the shared
    overflow bucket, taking the surface down for everyone.

    The test transport's peer is not a private address, so this exercises the
    directly-exposed branch: the header is ignored entirely.
    """
    monkeypatch.setattr(settings, "LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN", 2)

    for spoof in ("9.9.9.9", "8.8.8.8"):
        response = client.get(
            "/api/agent-start/version", headers={"X-Forwarded-For": spoof}
        )
        assert response.status_code == 200, response.text

    rotated = client.get("/api/agent-start/version", headers={"X-Forwarded-For": "7.7.7.7"})

    assert rotated.status_code == 429


def test_proxied_requests_are_keyed_on_the_hop_the_proxy_appended(
    monkeypatch,
) -> None:
    """Behind the local proxy, the *last* hop is the one nginx wrote.

    `$proxy_add_x_forwarded_for` appends the address it saw to whatever the
    client sent, so the client owns every earlier hop and cannot touch the last
    one. Taking the first hop (the audit helper's rule) would re-open the bypass
    even with a private-peer gate in front of it.
    """
    from app.api.routes import local_agent_kit
    from app.main import app
    from app.services.common.rate_limiter import RateLimiter

    monkeypatch.setattr(local_agent_kit, "_kit_rate_limiter", RateLimiter())
    monkeypatch.setattr(settings, "LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN", 2)

    # A private socket peer: the request arrived through our reverse proxy.
    proxied = TestClient(app, client=("10.1.2.3", 40000))

    # One real client (last hop 203.0.113.9) spoofing a different first hop on
    # every request still shares one budget.
    for spoof in ("1.1.1.1", "2.2.2.2"):
        response = proxied.get(
            "/api/agent-start/version",
            headers={"X-Forwarded-For": f"{spoof}, 203.0.113.9"},
        )
        assert response.status_code == 200, response.text

    blocked = proxied.get(
        "/api/agent-start/version", headers={"X-Forwarded-For": "3.3.3.3, 203.0.113.9"}
    )
    assert blocked.status_code == 429

    # A genuinely different client (different last hop) has its own budget.
    other = proxied.get(
        "/api/agent-start/version", headers={"X-Forwarded-For": "3.3.3.3, 198.51.100.4"}
    )
    assert other.status_code == 200


def test_rate_limit_covers_every_path_on_both_mounts(
    client: TestClient, monkeypatch
) -> None:
    """One budget per caller — not one per URL, which would be no budget."""
    monkeypatch.setattr(settings, "LOCAL_AGENT_KIT_RATE_LIMIT_PER_MIN", 2)

    assert client.get("/agent-start").status_code == 200
    assert client.get("/api/agent-start/kit.json").status_code == 200

    assert client.get("/api/agent-start/kit.tar.gz").status_code == 429
    assert client.get("/agent-start/START.md").status_code == 429


# ---------------------------------------------------------------------------
# Instance opt-out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/agent-start",
        "/agent-start/",
        "/api/agent-start",
        "/api/agent-start/START.md",
        "/api/agent-start/version",
        "/api/agent-start/kit.json",
        "/api/agent-start/kit.tar.gz",
        "/api/agent-start/kit/guides/11-go-cloud.md",
        # An instance that opted out publishes no contract either — the guard is
        # a router dependency, so it runs before either contract handler.
        "/agent-start/contract.tar.gz",
        "/api/agent-start/contract.tar.gz",
        "/agent-start/contract/version",
        "/api/agent-start/contract/version",
    ],
)
def test_disabled_instance_returns_404_everywhere(
    client: TestClient, monkeypatch, path: str
) -> None:
    """404, not 403: a disabled instance must not advertise the feature.

    A 403 would tell a scanner the surface exists here and is merely switched
    off, which is exactly what an operator opting out is declining to publish.
    """
    from app.services.cli.local_agent_kit_service import LocalAgentKitService

    monkeypatch.setattr(
        LocalAgentKitService, "is_enabled", staticmethod(lambda session: False)
    )

    response = client.get(path)

    assert response.status_code == 404, response.text


def test_enabled_by_default(client: TestClient) -> None:
    """No configuration required: a fresh instance publishes the kit.

    ``ServerConfig.local_agent_kit_enabled`` defaults to ``True`` in the model
    and to ``true`` in the column, so the singleton created lazily on this
    instance's first config read already publishes the kit.
    """
    assert client.get("/agent-start").status_code == 200


@pytest.mark.parametrize("mount", MOUNTS)
def test_admin_toggle_flips_both_mounts(
    client: TestClient, superuser_token_headers: dict[str, str], mount: str
) -> None:
    """The admin switch is what actually closes the surface, on both mounts.

    The disabled test above monkeypatches ``is_enabled``; this one drives the
    real path an operator uses — ``PUT /admin/server-config`` → column →
    ``ServerConfigService.get_or_create`` → the router guard — so a flag that
    is stored but never read, or read on only one mount, fails here.
    """
    assert client.get(mount).status_code == 200

    off = client.put(
        f"{settings.API_V1_STR}/admin/server-config",
        headers=superuser_token_headers,
        json={"local_agent_kit_enabled": False},
    )
    assert off.status_code == 200, off.text
    assert off.json()["local_agent_kit_enabled"] is False
    assert client.get(mount).status_code == 404

    on = client.put(
        f"{settings.API_V1_STR}/admin/server-config",
        headers=superuser_token_headers,
        json={"local_agent_kit_enabled": True},
    )
    assert on.status_code == 200, on.text
    assert client.get(mount).status_code == 200


# ---------------------------------------------------------------------------
# Missing snapshot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/agent-start",
        "/api/agent-start/START.md",
        "/api/agent-start/version",
        "/api/agent-start/kit.json",
        "/api/agent-start/kit.tar.gz",
        "/api/agent-start/kit/guides/11-go-cloud.md",
        # A snapshot that is absent entirely is a defect of the *kit*, not of
        # the contract, so it 503s before the contract's own coherence gate
        # (`_require_serviceable_contract`) is ever consulted. Both mattering
        # here is the point: the two contract routes must not be the one pair
        # that answers 200 with an empty archive.
        "/api/agent-start/contract.tar.gz",
        "/api/agent-start/contract/version",
    ],
)
def test_missing_snapshot_is_503_never_an_empty_200(
    client: TestClient, monkeypatch, tmp_path, path: str
) -> None:
    """An image built without `make sync-platform-knowledge` fails loud.

    Serving an empty-but-successful kit would be worse than an outage: the
    assistant would proceed with no instructions and improvise a layout that the
    cloud import cannot read.
    """
    from app.services.cli import local_agent_kit_service

    missing = tmp_path / "no-such-snapshot"
    monkeypatch.setattr(
        local_agent_kit_service, "local_kit_dir", lambda: missing
    )

    response = client.get(path)

    assert response.status_code == 503, response.text


def test_snapshot_recovers_after_a_missing_dir(client: TestClient) -> None:
    """The 503 path must not poison the cache for the next request."""
    assert client.get("/api/agent-start/version").status_code == 200


def test_instance_name_drops_surrounding_env_file_quotes(
    client: TestClient, monkeypatch
) -> None:
    """``PROJECT_NAME="Cinna"`` in .env reaches the container quoted; the kit
    must not print the quotes on an anonymous page, while an inner quote is kept."""
    from app.services.cli.local_agent_kit_service import LocalAgentKitService

    monkeypatch.setattr(settings, "PROJECT_NAME", '"Cinna"')
    monkeypatch.setattr(LocalAgentKitService, "_cache", None)
    assert client.get("/api/agent-start/kit.json").json()["instance_name"] == "Cinna"

    assert instance_display_name("Acme \"Labs\"") == 'Acme "Labs"'
    assert instance_display_name("  'Acme'  ") == "Acme"
    assert instance_display_name('""') == '""'
