"""Plugin snapshot source resolution — where an entry's files come from. Pure."""
import pytest

from app.services.plugins.plugin_snapshot_source import (
    SnapshotSource,
    Unsupported,
    git_source_key,
    resolve_source,
)


def _claude(source, name="p") -> dict:
    return {"name": name, "config": {"source": source}}


def test_string_source_is_local_with_normalised_subdir() -> None:
    src = resolve_source(_claude("./plugins/foo/"), "claude")
    assert isinstance(src, SnapshotSource)
    assert (src.kind, src.subdir, src.key) == ("local", "plugins/foo", "local:plugins/foo")


def test_url_source_with_sha_and_ref_fallback() -> None:
    src = resolve_source(
        _claude({"source": "url", "url": "https://github.com/acme/x.git", "sha": "a" * 40}),
        "claude",
    )
    assert isinstance(src, SnapshotSource)
    assert (src.kind, src.sha, src.subdir) == ("git", "a" * 40, "")
    by_branch = resolve_source(
        _claude({"source": "url", "url": "https://github.com/acme/x.git", "branch": "dev"}),
        "claude",
    )
    assert by_branch.ref == "dev" and by_branch.sha is None


def test_url_source_with_path_is_taken_from_the_subdir() -> None:
    src = resolve_source(
        _claude({"source": "url", "url": "https://github.com/acme/x.git", "path": "plugins/y"}),
        "claude",
    )
    assert src.subdir == "plugins/y"
    assert src.key.endswith("#plugins/y")


def test_git_subdir_and_github_shorthand() -> None:
    sub = resolve_source(
        _claude({"source": "git-subdir", "url": "https://github.com/acme/mono.git",
                 "path": "pkgs/a", "ref": "v1"}),
        "claude",
    )
    assert (sub.kind, sub.subdir, sub.ref) == ("git", "pkgs/a", "v1")
    gh = resolve_source(_claude({"source": "github", "repo": "acme/tool"}), "claude")
    assert gh.url == "https://github.com/acme/tool.git"
    assert gh.subdir == ""


@pytest.mark.parametrize(
    "source",
    [
        {"source": "npm", "package": "x"},
        {"source": "weird"},
        {"source": "github", "repo": "not a repo"},
        {"source": "url", "url": "ftp://example.com/x"},
        None,
        42,
    ],
)
def test_unknown_sources_are_unsupported(source) -> None:
    res = resolve_source(_claude(source), "claude")
    assert res == Unsupported("unknown_source")


def test_subdir_escaping_the_repository_is_unsafe_path() -> None:
    assert resolve_source(_claude("../outside"), "claude") == Unsupported("unsafe_path")
    res = resolve_source(
        _claude({"source": "git-subdir", "url": "https://github.com/a/b.git", "path": "../../x"}),
        "claude",
    )
    assert res == Unsupported("unsafe_path")


def test_codex_row_fields() -> None:
    ext = resolve_source(
        {"name": "c", "source_type": "url", "source_url": "https://github.com/acme/c.git",
         "source_path": "sub", "source_commit_hash": "b" * 40, "source_branch": "main"},
        "codex",
    )
    assert (ext.kind, ext.subdir, ext.sha, ext.ref) == ("git", "sub", "b" * 40, "main")
    local = resolve_source({"name": "c", "source_type": "local", "source_path": "plugins/c"}, "codex")
    assert (local.kind, local.subdir) == ("local", "plugins/c")
    parked = resolve_source(
        {"name": "c", "supported": False, "unsupported_reason": "npm_source"}, "codex"
    )
    assert parked == Unsupported("npm_source")


def test_ssh_and_https_spellings_share_one_key() -> None:
    https = resolve_source(_claude({"source": "url", "url": "https://github.com/acme/x.git"}), "claude")
    bare = resolve_source(_claude({"source": "url", "url": "https://github.com/acme/x"}), "claude")
    assert https.key == bare.key
    assert git_source_key("git@github.com:acme/x.git", "s") == git_source_key(
        "https://github.com/acme/x.git", "s"
    )


def test_skills_format_has_no_source() -> None:
    assert resolve_source(_claude("./x"), "skills") == Unsupported("unknown_source")
