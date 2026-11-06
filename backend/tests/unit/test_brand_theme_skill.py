"""The ``brand-theme`` catalog skill (``app/services/brand_theme/skill/brand-theme/``).

The skill ships a copy of the validator modules so an agent can check a
package offline. These tests keep the copy byte-identical to core, and check
each script the way an agent runs it: ``validate`` exit codes and output,
``mesh`` determinism and readability by construction, ``package`` producing a
zip core accepts, ``preview`` laying out every candidate in both modes, and
the built archive passing the catalog publish gate.

The scripts carry no ``.py`` extension (a core holding a published skill
restarts on ``.py`` files), so they are loaded with ``SourceFileLoader``.
Upload and publish of a theme through the API are covered in
``tests/api/brand_theme/``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

from app.scripts.build_brand_theme_skill import build_archive
from app.services.agents.skill_manifest import parse_skill_dir
from app.services.brand_theme import schema as S
from app.services.brand_theme.package import read_folder, read_zip
from app.services.brand_theme.svg_sanitizer import sanitise_svg
from app.services.brand_theme.validator import check_package
from app.services.skills.skill_catalog_service import SkillCatalogService
from app.services.skills.skill_upload import safe_extract

import numpy  # noqa: F401 — explicit dev dependency; the mesh generator needs it

CORE_DIR = Path(__file__).resolve().parents[2] / "app" / "services" / "brand_theme"
SKILL_DIR = CORE_DIR / "skill" / "brand-theme"
SCRIPTS = SKILL_DIR / "scripts"
VENDORED_DIR = SCRIPTS / "_lib" / "brand_theme"
TEMPLATE = SKILL_DIR / "template"
FIXTURES = CORE_DIR / "fixtures"
# The pure modules the skill copies verbatim (no FastAPI, DB or app imports).
VENDORED_MODULES = ("schema", "colors", "images", "svg_sanitizer", "validator", "package")
# Small enough to keep the suite fast, large enough for the raster size rule.
MESH_SIZE = (S.BACKGROUND_MIN_WIDTH, S.BACKGROUND_MIN_HEIGHT)


def _script(name: str) -> ModuleType:
    module_name = f"brand_theme_skill_script_{name}"
    if module_name not in sys.modules:
        loader = importlib.machinery.SourceFileLoader(module_name, str(SCRIPTS / name))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(module_name, loader))
        sys.modules[module_name] = module
        # No __pycache__ inside the skill folder (the scripts set this too).
        # Left on for the rest of the process; that only skips writing caches.
        sys.dont_write_bytecode = True
        loader.exec_module(module)
    return sys.modules[module_name]


def _package(tmp_path: Path, name: str = "acme") -> Path:
    target = tmp_path / name
    shutil.copytree(TEMPLATE, target, ignore=shutil.ignore_patterns("README.md"))
    return target


def _theme(package: Path) -> dict:
    return json.loads((package / "theme.json").read_text())


def _write_theme(package: Path, theme: dict) -> None:
    (package / "theme.json").write_text(json.dumps(theme, indent=2))


def _mesh(package: Path, out_dir: Path, **kwargs):
    kwargs.setdefault("size", MESH_SIZE)
    return _script("mesh").run(str(package), out_dir=str(out_dir), out=io.StringIO(), **kwargs)


# ── the copy cannot drift ──────────────────────────────────────────────

@pytest.mark.parametrize("module", VENDORED_MODULES)
def test_vendored_module_is_byte_identical_to_core(module: str) -> None:
    copy = VENDORED_DIR / module
    original = CORE_DIR / f"{module}.py"
    assert copy.read_bytes() == original.read_bytes(), (
        f"{copy} differs from {original}; copy it again: "
        f"cp {original} {copy}"
    )


def test_vendored_folder_holds_exactly_the_copied_modules() -> None:
    assert sorted(p.name for p in VENDORED_DIR.iterdir()) == sorted(VENDORED_MODULES)
    assert _script("validate").lib.VENDORED_MODULES == VENDORED_MODULES


def test_skill_ships_no_python_extension_files() -> None:
    offenders = [p for p in SKILL_DIR.rglob("*") if p.suffix in (".py", ".pyc")]
    assert offenders == []


# ── template ───────────────────────────────────────────────────────────

def test_template_passes_the_validator_without_warnings() -> None:
    theme, assets = read_folder(str(TEMPLATE))
    result = check_package(theme, assets)
    assert result.errors == []
    assert result.warnings == []
    assert set(result.assets) == {"assets/bg-dark.webp", "assets/bg-light.webp"}


def test_template_readme_describes_every_role() -> None:
    readme = (TEMPLATE / "README.md").read_text()
    for role, meaning in S.ROLE_MEANINGS.items():
        assert f"| `{role}` | {meaning} |" in readme, role


# ── validate ───────────────────────────────────────────────────────────

INVALID = sorted(p for p in (FIXTURES / "invalid").iterdir() if p.is_dir())


@pytest.mark.parametrize("case", INVALID, ids=[p.name for p in INVALID])
def test_validate_reports_exactly_the_fixture_errors(case: Path) -> None:
    out = io.StringIO()
    code = _script("validate").run(str(case), as_json=True, out=out)
    report = json.loads(out.getvalue())
    expected = json.loads((case / "expect.json").read_text())["errors"]
    assert code == 1
    assert report["ok"] is False
    assert sorted((e["path"], e["code"]) for e in report["errors"]) == sorted(
        (e["path"], e["code"]) for e in expected
    )


def _run_cli(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args], capture_output=True, text=True, timeout=120
    )


def test_validate_cli_valid_package_exits_0(tmp_path: Path) -> None:
    package = _package(tmp_path)
    proc = _run_cli("validate", str(package))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "VALID: 0 errors, 0 warnings."


def test_validate_cli_invalid_package_exits_1_with_path_code_and_fix() -> None:
    proc = _run_cli("validate", str(FIXTURES / "invalid" / "low-contrast-text"))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0].startswith("ERROR modes.dark.text.muted [low_contrast]: ")
    assert "needs" in lines[0]
    assert lines[-1] == "INVALID: 1 error, 0 warnings."


def test_validate_cli_unreadable_package_exits_2(tmp_path: Path) -> None:
    proc = _run_cli("validate", str(tmp_path / "nothing-here"))
    assert proc.returncode == 2
    assert proc.stdout.startswith("ERROR package [unreadable]: ")


def test_validate_reads_a_zip_like_core(tmp_path: Path) -> None:
    package = _package(tmp_path)
    assert _script("package").run(str(package), str(tmp_path / "acme.zip"), out=io.StringIO()) == 0
    assert _script("validate").run(str(tmp_path / "acme.zip"), out=io.StringIO()) == 0


# ── mesh ───────────────────────────────────────────────────────────────

def test_mesh_is_deterministic_for_a_seed(tmp_path: Path) -> None:
    package = _package(tmp_path)
    first, second = tmp_path / "a", tmp_path / "b"
    assert _mesh(package, first, modes=["dark"], seed=3)[0] == 0
    assert _mesh(package, second, modes=["dark"], seed=3)[0] == 0
    assert (first / "bg-dark-s3.webp").read_bytes() == (second / "bg-dark-s3.webp").read_bytes()
    _mesh(package, first, modes=["dark"], seed=4)
    assert (first / "bg-dark-s4.webp").read_bytes() != (first / "bg-dark-s3.webp").read_bytes()


def test_mesh_layout_is_shared_by_both_modes() -> None:
    mesh = _script("mesh")
    assert mesh.layout(11, 0.6, 3) == mesh.layout(11, 0.6, 3)
    assert mesh.layout(11, 0.6, 3) != mesh.layout(12, 0.6, 3)


@pytest.mark.parametrize("fmt", ["webp", "svg"])
def test_mesh_auto_with_stock_roles_passes_the_validator_in_both_modes(tmp_path: Path, fmt: str) -> None:
    package = _package(tmp_path)
    code, results = _mesh(package, package / "assets", modes=["dark", "light"], seed=5, fmt=fmt)
    assert code == 0
    assert {r.mode for r in results} == {"dark", "light"}
    assert all(r.errors == [] and r.strength is not None and 0 < r.strength <= 1 for r in results)

    theme = _theme(package)
    theme["background"] = {mode: f"assets/bg-{mode}-s5.{fmt}" for mode in ("dark", "light")}
    _write_theme(package, theme)
    result = check_package(*read_folder(str(package)))
    assert result.errors == []


def test_mesh_auto_says_the_roles_must_change_when_text_is_too_weak(tmp_path: Path) -> None:
    package = _package(tmp_path)
    theme = _theme(package)
    theme["modes"]["dark"]["text.secondary"] = "#3a3a3a"  # about 1.7:1 on #0f0f0f
    _write_theme(package, theme)
    out = io.StringIO()
    code, results = _script("mesh").run(
        str(package), ["dark"], seed=1, out_dir=str(tmp_path / "out"), size=MESH_SIZE, out=out
    )
    assert code == 1
    assert results[0].roles_must_change is True
    assert "text roles, not the image, need changing" in out.getvalue()
    assert "text.secondary" in out.getvalue()
    assert not (tmp_path / "out" / "bg-dark-s1.webp").exists()


def test_mesh_svg_passes_the_sanitiser_unchanged(tmp_path: Path) -> None:
    package = _package(tmp_path)
    _mesh(package, tmp_path / "out", modes=["dark", "light"], seed=2, fmt="svg")
    for mode in ("dark", "light"):
        data = (tmp_path / "out" / f"bg-{mode}-s2.svg").read_bytes()
        sanitised = sanitise_svg(data)
        assert sanitised.removed == []
        assert sanitised.data == data


def test_mesh_fixed_strength_that_fails_is_reported(tmp_path: Path) -> None:
    package = _package(tmp_path)
    code, results = _mesh(package, tmp_path / "out", modes=["light"], seed=2, strength=1.0,
                          colors="#000000")
    assert code == 1
    assert results[0].path is not None
    assert any("text.primary" in message or "text.secondary" in message for message in results[0].errors)


def test_mesh_color_list_takes_roles_and_literal_colors() -> None:
    lib = _script("mesh").lib
    assert lib.split_color_list("accent, rgb(1, 2, 3),#7a3cff") == ["accent", "rgb(1, 2, 3)", "#7a3cff"]


# ── package ────────────────────────────────────────────────────────────

def test_package_writes_a_zip_core_accepts_with_only_referenced_assets(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (package / "assets" / "bg-dark-s9.webp").write_bytes((package / "assets" / "bg-dark.webp").read_bytes())
    out = io.StringIO()
    assert _script("package").run(str(package), out=out) == 0
    data = (tmp_path / "acme.zip").read_bytes()

    theme, assets = read_zip(data)
    result = check_package(theme, assets)
    assert result.errors == []
    assert result.warnings == []
    assert sorted(assets) == ["assets/bg-dark.webp", "assets/bg-light.webp"]
    assert "bg-dark-s9.webp" in out.getvalue()


def test_package_refuses_an_invalid_package_and_writes_nothing(tmp_path: Path) -> None:
    package = tmp_path / "broken"
    shutil.copytree(FIXTURES / "invalid" / "low-contrast-text", package)
    out = io.StringIO()
    assert _script("package").run(str(package), out=out) == 1
    assert "[low_contrast]" in out.getvalue()
    assert not (tmp_path / "broken.zip").exists()


# ── preview ────────────────────────────────────────────────────────────

def test_preview_has_both_modes_and_one_row_per_candidate(tmp_path: Path) -> None:
    package = _package(tmp_path)
    _mesh(package, package / "assets", modes=["dark", "light"], seed=1, candidates=2, fmt="svg")
    out_file = tmp_path / "preview.html"
    assert _script("preview").run(str(package), out_path=str(out_file), out=io.StringIO()) == 0
    page = out_file.read_text()

    rows = page.split('<section class="row" ')[1:]
    labels = [row.split('data-candidate="', 1)[1].split('"', 1)[0] for row in rows]
    assert labels == ["theme.json background", "mesh seed 1 (svg)", "mesh seed 2 (svg)"]
    for row in rows:
        assert row.count('data-mode="dark"') == 1
        assert row.count('data-mode="light"') == 1
        assert "passes the validator" in row
    # Images are embedded, so the file can be handed over on its own.
    assert "data:image/webp;base64," in page and "data:image/svg+xml;base64," in page


def test_preview_marks_a_refused_background(tmp_path: Path) -> None:
    package = _package(tmp_path)
    theme = _theme(package)
    # Swap the stock images: the dark one is now the light one.
    theme["background"] = {"dark": "assets/bg-light.webp", "light": "assets/bg-dark.webp"}
    _write_theme(package, theme)
    out_file = tmp_path / "preview.html"
    _script("preview").run(str(package), out_path=str(out_file), out=io.StringIO())
    assert 'class="verdict fail"' in out_file.read_text()


# ── publishing ─────────────────────────────────────────────────────────

def test_built_archive_passes_the_catalog_publish_gate(tmp_path: Path) -> None:
    """What ``POST /skills/packages/publish`` runs before touching the DB:
    safe extraction, the skill parser and the three content refusals."""
    data = build_archive()
    assert data == build_archive()  # deterministic

    name = safe_extract(data, tmp_path)
    assert name == "brand-theme"
    entry = parse_skill_dir(tmp_path / name, rel_path=f"skills/{name}")
    SkillCatalogService.validate_entry(entry)
    assert entry.name == "brand-theme"
    assert entry.has_scripts is True
    assert entry.secret_paths == []

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        modes = {m.name: m.mode for m in tar.getmembers() if m.isfile()}
    assert not any(n.endswith((".py", ".pyc")) or "__pycache__" in n for n in names)
    for script in ("validate", "mesh", "preview", "package"):
        assert modes[f"brand-theme/scripts/{script}"] & 0o111, script


def test_mesh_cli_writes_a_readable_svg_pair(tmp_path: Path) -> None:
    package = _package(tmp_path)
    proc = _run_cli("mesh", str(package), "--mode", "both", "--seed", "4", "--strength", "0.3",
                    "--softness", "0.7", "--format", "svg", "--out", str(tmp_path / "out"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["bg-dark-s4.svg", "bg-light-s4.svg"]
    assert "Move the chosen files into" in proc.stdout


def test_mesh_cli_rejects_bad_arguments(tmp_path: Path) -> None:
    package = _package(tmp_path)
    assert _run_cli("mesh", str(package), "--strength", "1.5").returncode == 2
    assert _run_cli("mesh", str(package), "--candidates", "0").returncode == 2


def test_mesh_reports_a_rendering_failure_as_is_not_as_a_roles_problem(tmp_path: Path, monkeypatch) -> None:
    mesh = _script("mesh")
    package = _package(tmp_path)
    corrupt = mesh.lib.load().validator.ValidationError("background.dark", "asset_corrupt", "could not be rendered")
    monkeypatch.setattr(mesh.MeshMaker, "check", lambda self, data: [corrupt])
    out = io.StringIO()
    code, results = mesh.run(str(package), ["dark"], seed=1, out_dir=str(tmp_path / "out"), size=MESH_SIZE, out=out)
    assert code == 1
    assert results[0].roles_must_change is False
    assert "could not be checked" in out.getvalue()
    assert "text roles" not in out.getvalue()


def test_preview_does_not_call_a_missing_background_accepted(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (package / "assets" / "bg-light.webp").unlink()
    out_file = tmp_path / "preview.html"
    _script("preview").run(str(package), out_path=str(out_file), out=io.StringIO())
    first_row = out_file.read_text().split('<section class="row" ')[1]
    assert 'class="verdict fail"' in first_row
    assert "not in the package" in first_row


def test_preview_says_not_measured_when_text_colors_do_not_parse(tmp_path: Path) -> None:
    package = _package(tmp_path)
    theme = _theme(package)
    theme["modes"]["dark"]["text.primary"] = "white"
    _write_theme(package, theme)
    out_file = tmp_path / "preview.html"
    _script("preview").run(str(package), out_path=str(out_file), out=io.StringIO())
    assert "Not measured: fix modes.dark.text.primary first" in out_file.read_text()
    assert "passes the validator with these colors" not in out_file.read_text()


def test_validate_passes_core_package_reader_codes_through(tmp_path: Path) -> None:
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    out = io.StringIO()
    assert _script("validate").run(str(tmp_path / "bad.zip"), as_json=True, out=out) == 2
    assert json.loads(out.getvalue())["errors"][0]["code"] == "invalid_package"
