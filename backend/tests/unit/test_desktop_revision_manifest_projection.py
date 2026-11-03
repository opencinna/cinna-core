"""M7 — ``project_revision_manifest`` keeps ``marketplace_skills`` pins (plan D8).

The revision manifest is not exposed through the API, so the projection is
asserted directly.
"""
from app.services.catalog.desktop_agent_revisions import project_revision_manifest

PIN = {
    "entry_uuid": "11111111-2222-4333-8444-555555555555",
    "marketplace_uuid": "66666666-7777-4888-9999-000000000000",
    "marketplace_name": "Team Market",
    "name": "market-report",
    "archive_sha256": "a" * 64,
    "future_key": {"kept": True},
}


def test_marketplace_pins_survive_the_projection_unchanged() -> None:
    out = project_revision_manifest({"marketplace_skills": [PIN, "junk", 3]})
    assert out["marketplace_skills"] == [PIN]


def test_a_manifest_without_pins_projects_an_empty_list() -> None:
    assert project_revision_manifest({}).get("marketplace_skills") == []
    assert project_revision_manifest({"marketplace_skills": None}).get("marketplace_skills") == []


def test_authored_skills_summary_flags_dashboards(tmp_path) -> None:
    from app.services.catalog.desktop_agent_revisions import _authored_skills

    for name in ("metrics", "plain"):
        skill = tmp_path / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Does {name} things.\n---\nBody\n",
            encoding="utf-8",
        )
    board = tmp_path / "metrics" / "dashboards" / "weekly"
    board.mkdir(parents=True)
    (board / "component.json").write_text("{}\n", encoding="utf-8")

    summary = {entry["name"]: entry for entry in _authored_skills(tmp_path)}

    assert summary["metrics"]["has_dashboards"] is True
    assert summary["plain"]["has_dashboards"] is False
    assert summary["plain"]["source"] == "authored"
