"""Minting a desktop agent's catalog id: ``BundleIdService.desktop_bundle_id``.

Pure (no DB, no settings): ``<prefix>.<8 hex of the manifest id>``, or the first
free ``-2``, ``-3``, ... suffix against ``taken``. The same function mints ids in
``POST /catalog/desktop-agents`` and in the ``desktop_bundles`` migration.

API-observable path (minted shape, collision suffix through two creates):
``tests/api/catalog/catalog_desktop_agents_test.py``.
"""
import uuid

import pytest

from app.services.bundles.bundle_id_service import BUNDLE_ID_REGEX, BundleIdService

PREFIX = "io.example.cinna"
MANIFEST = uuid.UUID("a1b2c3d4-0000-4000-8000-0000000000ff")
BASE = f"{PREFIX}.a1b2c3d4"


def test_free_id_is_prefix_dot_first_eight_hex_of_manifest_id() -> None:
    assert BundleIdService.desktop_bundle_id(PREFIX, MANIFEST, set()) == BASE


def test_only_the_first_eight_hex_are_used_and_lowercased() -> None:
    upper = uuid.UUID("ABCDEF12-3456-4789-8abc-def012345678")
    assert BundleIdService.desktop_bundle_id(PREFIX, upper, set()) == f"{PREFIX}.abcdef12"


def test_unrelated_taken_ids_do_not_matter() -> None:
    taken = {f"{PREFIX}.ffffffff", f"{BASE}x", "other.a1b2c3d4"}
    assert BundleIdService.desktop_bundle_id(PREFIX, MANIFEST, taken) == BASE


@pytest.mark.parametrize(
    "taken, expected",
    [
        ({BASE}, f"{BASE}-2"),
        ({BASE, f"{BASE}-2"}, f"{BASE}-3"),
        ({BASE, f"{BASE}-2", f"{BASE}-3", f"{BASE}-4"}, f"{BASE}-5"),
        # The first free suffix, not one past the highest taken.
        ({BASE, f"{BASE}-3"}, f"{BASE}-2"),
        # A suffix without the base being taken: the base is free.
        ({f"{BASE}-2"}, BASE),
    ],
)
def test_collision_appends_first_free_numeric_suffix(taken: set[str], expected: str) -> None:
    assert BundleIdService.desktop_bundle_id(PREFIX, MANIFEST, taken) == expected


@pytest.mark.parametrize("prefix", [PREFIX, "localhost", "5.1.168.192", "io.opencinna.cinna"])
@pytest.mark.parametrize("taken", [set(), {f"{{p}}.a1b2c3d4"}])
def test_minted_ids_match_bundle_id_regex(prefix: str, taken: set[str]) -> None:
    taken = {t.replace("{p}", prefix) for t in taken}
    minted = BundleIdService.desktop_bundle_id(prefix, MANIFEST, taken)
    assert minted.startswith(f"{prefix}.a1b2c3d4")
    assert BUNDLE_ID_REGEX.match(minted)
    assert BundleIdService.is_valid_format(minted)


def test_does_not_mutate_taken() -> None:
    taken = {BASE}
    BundleIdService.desktop_bundle_id(PREFIX, MANIFEST, taken)
    assert taken == {BASE}
