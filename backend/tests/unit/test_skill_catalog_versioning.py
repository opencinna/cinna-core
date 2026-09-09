"""Unit tests: the version and package-id a skill publish derives for itself.

Three pure pieces, none of which needs a database or a workspace:

* ``resolve_version`` — the one sentence the publish dialog and the publish
  itself both obey ("the header's version, unless it has already been
  published — then the next one after the newest release").
* ``_write_version_to_skill_md`` — a surgical frontmatter edit that must not
  disturb a single other byte of an author's file, because it runs on the
  user's own workspace.
* ``base_package_id`` — the reverse-DNS shape. The collision walk on top of it
  (``derive_package_id``) reads the catalog and is covered through the API.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.agents.skill_manifest import (
    coerce_version,
    parse_frontmatter,
    parse_skill_dir,
)
from app.services.skills.skill_catalog_service import SkillCatalogService


def _resolve(**kwargs) -> str:
    defaults = {
        "requested": None,
        "frontmatter_version": None,
        "published_versions": set(),
        "latest_version": None,
    }
    return SkillCatalogService.resolve_version(**{**defaults, **kwargs})


# ── resolve_version ────────────────────────────────────────────────────


def test_first_publish_of_an_unversioned_skill_starts_at_1_0_0():
    assert _resolve() == "1.0.0"


def test_an_unpublished_header_version_is_honoured_as_written():
    # The author said 2.1.0 and nothing has claimed it: publishing it as
    # anything else would silently overrule them.
    assert _resolve(frontmatter_version="2.1.0") == "2.1.0"


def test_a_header_version_that_is_already_published_bumps_instead():
    # The ordinary re-publish: the header holds what the last publish wrote
    # back, so continuing the series is the only non-duplicating answer.
    assert (
        _resolve(
            frontmatter_version="1.0.0",
            published_versions={"1.0.0"},
            latest_version="1.0.0",
        )
        == "1.0.1"
    )


def test_the_series_continues_from_the_newest_release_not_the_header():
    # A header left behind by a hand-edit must not restart the series at 1.0.1
    # when 1.0.1 has already shipped.
    assert (
        _resolve(
            frontmatter_version="1.0.0",
            published_versions={"1.0.0", "1.0.1"},
            latest_version="1.0.1",
        )
        == "1.0.2"
    )


def test_a_free_number_after_the_newest_release_is_taken_even_if_later_ones_exist():
    # `latest_version` is the newest revision's version, not the highest
    # string: a hand-typed 1.0.2 sitting on an older revision does not push
    # the series past the 1.0.1 that is genuinely free.
    assert (
        _resolve(
            published_versions={"1.0.0", "1.0.2"},
            latest_version="1.0.0",
        )
        == "1.0.1"
    )


def test_a_bump_that_lands_on_a_taken_version_keeps_walking():
    # A publisher who typed 1.0.1 by hand on an earlier revision leaves the
    # obvious successor occupied; the series steps over it.
    assert (
        _resolve(
            published_versions={"1.0.0", "1.0.1"},
            latest_version="1.0.0",
        )
        == "1.0.2"
    )


def test_an_explicit_request_wins_and_is_never_deduplicated():
    # Two revisions may legitimately carry one version — a re-publish of the
    # same release — and the publisher typed it.
    assert (
        _resolve(
            requested="1.0.0",
            frontmatter_version="9.9.9",
            published_versions={"1.0.0"},
            latest_version="1.0.0",
        )
        == "1.0.0"
    )


def test_an_explicit_request_is_trimmed():
    assert _resolve(requested="  2.0.0  ") == "2.0.0"


def test_a_blank_request_falls_through_to_the_derivation():
    # The dialog posts `null` for an emptied field, but a client that posts
    # whitespace must not publish a whitespace version.
    assert _resolve(requested="   ", frontmatter_version="3.0.0") == "3.0.0"


def test_versions_that_are_not_semver_still_have_a_successor():
    # The frontmatter `version` is free text by the Agent Skills standard, so
    # the bump increments the last run of digits rather than parsing semver.
    assert _resolve(published_versions={"1.2"}, latest_version="1.2") == "1.3"
    assert _resolve(published_versions={"v3"}, latest_version="v3") == "v4"
    assert (
        _resolve(published_versions={"2026-09-09"}, latest_version="2026-09-09")
        == "2026-09-10"
    )
    assert _resolve(published_versions={"1.0.9"}, latest_version="1.0.9") == "1.0.10"


def test_a_version_with_no_digits_gets_a_suffix_rather_than_a_restart():
    # "alpha" has no digits to increment, so there is no obvious successor.
    # Appending `.1` keeps the series moving *upward*; restarting at `1.0.0`
    # (what this did before the 2026-09-09 review) published a next revision
    # numbered below its own predecessor.
    assert (
        _resolve(published_versions={"alpha"}, latest_version="alpha")
        == "alpha.1"
    )


def test_unversioned_earlier_revisions_do_not_break_the_derivation():
    # Revisions published before skills carried versions have `version=None`,
    # so the history is a set of nothing and the header is the only seed.
    assert _resolve(frontmatter_version="0.1.0") == "0.1.0"


# ── _write_version_to_skill_md ─────────────────────────────────────────


def _skill_md(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_version_is_inserted_below_name(tmp_path: Path):
    path = _skill_md(
        tmp_path,
        "---\nname: pdf-report\ndescription: Makes PDFs\n---\n\n# Body\ntext\n",
    )
    assert SkillCatalogService._write_version_to_skill_md(path, "1.0.0") is True
    assert path.read_text(encoding="utf-8") == (
        "---\nname: pdf-report\nversion: 1.0.0\ndescription: Makes PDFs\n"
        "---\n\n# Body\ntext\n"
    )


def test_an_existing_version_is_replaced_in_place(tmp_path: Path):
    path = _skill_md(
        tmp_path,
        "---\nname: pdf-report\nversion: 0.9\ndescription: d\n---\nbody\n",
    )
    assert SkillCatalogService._write_version_to_skill_md(path, "1.0.0") is True
    assert "version: 1.0.0" in path.read_text(encoding="utf-8")
    assert "0.9" not in path.read_text(encoding="utf-8")


def test_writing_the_version_it_already_has_changes_nothing(tmp_path: Path):
    text = "---\nname: pdf-report\nversion: 1.0.0\ndescription: d\n---\nbody\n"
    path = _skill_md(tmp_path, text)
    # True because the file carries the version afterwards — the caller only
    # warns on a genuine failure to write.
    assert SkillCatalogService._write_version_to_skill_md(path, "1.0.0") is True
    assert path.read_text(encoding="utf-8") == text


def test_the_edit_survives_a_round_trip_through_the_parser(tmp_path: Path):
    path = _skill_md(
        tmp_path,
        "---\nname: pdf-report\ndescription: d\nallowed-tools: [Bash, Read]\n"
        "---\n\nBody stays.\n",
    )
    SkillCatalogService._write_version_to_skill_md(path, "1.0.0")
    frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["name"] == "pdf-report"
    # The author's other keys and their shapes are untouched.
    assert frontmatter["allowed-tools"] == ["Bash", "Read"]
    assert body == "Body stays."


def test_crlf_line_endings_are_preserved(tmp_path: Path):
    path = _skill_md(
        tmp_path, "---\r\nname: pdf-report\r\ndescription: d\r\n---\r\nbody\r\n"
    )
    SkillCatalogService._write_version_to_skill_md(path, "1.0.0")
    assert path.read_bytes() == (
        b"---\r\nname: pdf-report\r\nversion: 1.0.0\r\ndescription: d\r\n"
        b"---\r\nbody\r\n"
    )


def test_a_byte_order_mark_is_preserved(tmp_path: Path):
    path = tmp_path / "SKILL.md"
    path.write_bytes(
        "﻿---\nname: pdf-report\ndescription: d\n---\nbody\n".encode()
    )
    SkillCatalogService._write_version_to_skill_md(path, "1.0.0")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "version: 1.0.0" in path.read_text(encoding="utf-8-sig")


def test_an_indented_version_belongs_to_the_author_and_is_left_alone(
    tmp_path: Path,
):
    path = _skill_md(
        tmp_path,
        "---\nname: pdf-report\ndescription: d\nmeta:\n  version: 3\n---\nbody\n",
    )
    SkillCatalogService._write_version_to_skill_md(path, "1.0.0")
    text = path.read_text(encoding="utf-8")
    assert "  version: 3" in text
    assert "\nversion: 1.0.0\n" in text


def test_a_file_with_no_frontmatter_is_refused_rather_than_rewritten(
    tmp_path: Path,
):
    path = _skill_md(tmp_path, "no frontmatter here\n")
    assert SkillCatalogService._write_version_to_skill_md(path, "1.0.0") is False
    assert path.read_text(encoding="utf-8") == "no frontmatter here\n"


def test_an_unterminated_fence_is_refused(tmp_path: Path):
    path = _skill_md(tmp_path, "---\nname: pdf-report\ndescription: d\n")
    assert SkillCatalogService._write_version_to_skill_md(path, "1.0.0") is False


def test_a_missing_file_is_refused_without_raising(tmp_path: Path):
    assert (
        SkillCatalogService._write_version_to_skill_md(
            tmp_path / "nope" / "SKILL.md", "1.0.0"
        )
        is False
    )


def test_no_temporary_file_is_left_behind(tmp_path: Path):
    path = _skill_md(
        tmp_path, "---\nname: pdf-report\ndescription: d\n---\nbody\n"
    )
    SkillCatalogService._write_version_to_skill_md(path, "1.0.0")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["SKILL.md"]


def test_the_written_version_is_what_the_index_then_reports(tmp_path: Path):
    # The whole point of the write-back: the row's version badge and the
    # published revision are the same number because they read the same line.
    skill_dir = tmp_path / "pdf-report"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pdf-report\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    assert parse_skill_dir(skill_dir).version is None
    SkillCatalogService._write_version_to_skill_md(
        skill_dir / "SKILL.md", "1.0.1"
    )
    assert parse_skill_dir(skill_dir).version == "1.0.1"


# ── package ids ────────────────────────────────────────────────────────


def test_the_base_package_id_is_reverse_dns_with_a_skill_segment():
    package_id = SkillCatalogService.base_package_id("pdf-report")
    assert package_id.endswith(".skill.pdf-report")
    # The same prefix bundle ids get, so the two families read as one
    # namespace rather than two conventions.
    from app.services.bundles.bundle_id_service import (
        BUNDLE_ID_REGEX,
        BundleIdService,
    )

    assert package_id.startswith(f"{BundleIdService.reversed_host_prefix()}.")
    assert BUNDLE_ID_REGEX.match(package_id)


# ── frontmatter coercion ───────────────────────────────────────────────


def test_unquoted_numeric_versions_survive_as_strings():
    # `_coerce_scalar` turns `version: 2` into an int and `version: 1.0` into a
    # float before this sees them; dropping either would lose a version the
    # author plainly wrote.
    assert coerce_version(2) == "2"
    assert coerce_version(1.0) == "1.0"
    assert coerce_version("1.0.0") == "1.0.0"


def test_non_scalar_and_empty_versions_are_absent_rather_than_wrong():
    assert coerce_version(None) is None
    assert coerce_version(True) is None
    assert coerce_version(["1.0.0"]) is None
    assert coerce_version("   ") is None


# ── Regressions from the 2026-09-09 verification round ─────────────────


def test_a_version_carrying_a_newline_cannot_inject_frontmatter():
    # `version:` is written verbatim into a frontmatter block, so a newline
    # would add top-level keys to the author's own header — a `name:` that no
    # longer matches the folder bricks the skill, and the revision is
    # immutable, so it ships. Refused, not escaped.
    evil = "1.0.0\nname: evil\nuser-invocable: false"
    assert coerce_version(evil) is None
    # It therefore falls through to the derivation rather than publishing a
    # blank or the raw string.
    assert _resolve(requested=evil) == "1.0.0"


def test_control_characters_are_refused_at_the_request_boundary():
    from pydantic import ValidationError

    from app.models.skills.schemas import SkillPublishRequest

    assert SkillPublishRequest(version="1.0.0").version == "1.0.0"
    assert SkillPublishRequest().version is None
    for bad in ("1.0.0\nname: evil", "1.0.0\r\nx: y", "1.0.0\x00"):
        with pytest.raises(ValidationError):
            SkillPublishRequest(version=bad)


def test_the_written_file_stays_a_single_version_line(tmp_path: Path):
    # The end-to-end guarantee: whatever a caller sends, the header gains one
    # line and the parser sees one version.
    path = _skill_md(
        tmp_path, "---\nname: pdf-report\ndescription: Real.\n---\nbody\n"
    )
    resolved = _resolve(requested="1.0.0\nname: evil")
    SkillCatalogService._write_version_to_skill_md(path, resolved)
    frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert frontmatter["name"] == "pdf-report"
    assert "evil" not in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").count("version:") == 1


def test_a_version_with_no_successor_continues_above_its_predecessor():
    # Regression: the fallback used to be `1.0.0` regardless of history, so a
    # package whose latest release was `2.0.0-beta` published its NEXT revision
    # as `1.0.0` — lower than its predecessor — and stamped that into the
    # author's header.
    assert (
        _resolve(
            published_versions={"2.0.0-beta"}, latest_version="2.0.0-beta"
        )
        == "2.0.0-beta.1"
    )
    # A package with no releases at all still starts a fresh series — the
    # `.1` rule applies only where there is a predecessor to stay above.
    assert _resolve(frontmatter_version="alpha") == "alpha"
    assert _resolve() == "1.0.0"
