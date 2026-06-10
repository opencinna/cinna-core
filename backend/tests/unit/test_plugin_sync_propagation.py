"""Bundle-propagated plugin reconcile tests (service-level).

Covers ``app.services.bundles.plugin_sync`` — the snapshot + materialise + merge
engine that propagates a publisher's plugins through a bundle revision to a
consumer install as ``source=bundle`` ``AgentPluginLink`` rows.

The full publish→install→apply-update API flow for plugins additionally needs a
marketplace + running agent env (to install + materialise plugin files), which is
exercised elsewhere; these tests pin the reconcile semantics directly against the
DB so the behavioral-signature merge contract is locked:

  1. snapshot_plugin_specs projects marketplace + bundle links uniformly.
  2. materialise creates source=bundle links with published flags + config.
  3. merge: unchanged plugin keeps the consumer's disabled/per-mode toggles and
     refreshes frozen version/config; changed→reinstall; added→create;
     removed→delete. ONLY source=bundle links are touched.
  4. A bundle plugin colliding with a consumer source=marketplace plugin of the
     same (mkt, name) is skipped.

All DB access goes through the ``db`` session fixture (no network / API / TestClient).
This is a service-level test, not a pure-logic unit test: it uses the shared ``db``
fixture and therefore requires the migrated ``app_test`` database (see the
"service-level tests" note in tests/unit/README.md). The full
publish→install→apply-update API flow that also reaches these merge scenarios is
future coverage (it needs a marketplace + running agent env).
"""
import uuid

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.plugins.llm_plugin import (
    AgentPluginLink,
    LLMPluginMarketplace,
    LLMPluginMarketplacePlugin,
    PluginSource,
)
from app.models.users.user import UserCreate
from app.services.bundles import plugin_sync
from app.services.users.user_service import UserService


# ── Fixtures-as-helpers ───────────────────────────────────────────────────────


def _make_user(db: Session) -> uuid.UUID:
    user = UserService.create_user(
        session=db,
        user_create=UserCreate(
            email=f"plug-{uuid.uuid4().hex[:8]}@example.com",
            password="testpassword123",
        ),
    )
    return user.id


def _make_install(db: Session, owner_id: uuid.UUID) -> Agent:
    install = Agent(
        name="Consumer Install",
        owner_id=owner_id,
        bundle_id=f"bundle-{uuid.uuid4().hex[:8]}",
    )
    db.add(install)
    db.commit()
    db.refresh(install)
    return install


def _make_revision(
    db: Session, owner_id: uuid.UUID, plugin_specs: list[dict], revision_number: int = 1
) -> AgentBundleRevision:
    bundle = AgentBundle(
        bundle_id=f"bundle-{uuid.uuid4().hex[:8]}",
        display_name="Test Bundle",
        publisher_user_id=owner_id,
    )
    db.add(bundle)
    db.commit()
    db.refresh(bundle)

    rev = AgentBundleRevision(
        bundle_id=bundle.id,
        revision_number=revision_number,
        snapshot_path="/tmp/none",
        content_hash="x" * 64,
        plugin_specs=plugin_specs,
    )
    db.add(rev)
    db.commit()
    db.refresh(rev)
    return rev


def _spec(
    mkt: str,
    name: str,
    *,
    version: str = "1.0",
    commit: str = "abc",
    conversation: bool = True,
    building: bool = True,
    disabled: bool = False,
    config: dict | None = None,
) -> dict:
    return {
        "marketplace_name": mkt,
        "plugin_name": name,
        "version": version,
        "commit_hash": commit,
        "conversation_mode": conversation,
        "building_mode": building,
        "disabled": disabled,
        "config": config or {"name": name},
        "snapshot_subdir": f"plugins/{mkt}/{name}",
    }


def _bundle_links(db: Session, agent_id: uuid.UUID) -> list[AgentPluginLink]:
    return list(
        db.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.agent_id == agent_id,
                AgentPluginLink.source == PluginSource.bundle,
            )
        ).all()
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_materialise_creates_bundle_links(db: Session) -> None:
    """materialise creates one source=bundle link per spec with published flags."""
    install = _make_install(db, _make_user(db))
    rev = _make_revision(
        db,
        install.owner_id,
        [
            _spec("acme", "pdf", conversation=True, building=False, disabled=False),
            _spec("acme", "csv", conversation=True, building=True, disabled=True),
        ],
    )

    created = plugin_sync.materialise(db, install, rev)
    db.commit()
    assert created == 2

    links = {l.snapshot_plugin_name: l for l in _bundle_links(db, install.id)}
    assert set(links) == {"pdf", "csv"}
    assert links["pdf"].source == PluginSource.bundle
    assert links["pdf"].plugin_id is None
    assert links["pdf"].snapshot_marketplace_name == "acme"
    assert links["pdf"].building_mode is False
    assert links["csv"].disabled is True
    assert links["pdf"].snapshot_config == {"name": "pdf"}


def test_merge_unchanged_preserves_consumer_toggles(db: Session) -> None:
    """A behaviorally-unchanged plugin keeps the user's toggles; metadata refreshes."""
    install = _make_install(db, _make_user(db))
    rev1 = _make_revision(db, install.owner_id, [_spec("acme", "pdf", version="1.0", config={"name": "pdf", "v": 1})])
    plugin_sync.materialise(db, install, rev1)
    db.commit()

    # Consumer disables the plugin and turns off building mode.
    link = _bundle_links(db, install.id)[0]
    link.disabled = True
    link.building_mode = False
    db.add(link)
    db.commit()

    # New revision: same (mkt, name) identity, bumped version + config.
    rev2 = _make_revision(db, install.owner_id, [_spec("acme", "pdf", version="2.0", config={"name": "pdf", "v": 2})])
    plugin_sync.merge(db, install, rev2)

    links = _bundle_links(db, install.id)
    assert len(links) == 1
    refreshed = links[0]
    # Toggles preserved...
    assert refreshed.disabled is True
    assert refreshed.building_mode is False
    # ...metadata refreshed.
    assert refreshed.installed_version == "2.0"
    assert refreshed.snapshot_config == {"name": "pdf", "v": 2}


def test_merge_added_and_removed(db: Session) -> None:
    """merge creates added plugins and deletes removed ones."""
    install = _make_install(db, _make_user(db))
    rev1 = _make_revision(db, install.owner_id, [_spec("acme", "pdf"), _spec("acme", "csv")])
    plugin_sync.materialise(db, install, rev1)
    db.commit()
    assert {l.snapshot_plugin_name for l in _bundle_links(db, install.id)} == {"pdf", "csv"}

    # New revision drops csv, adds xls.
    rev2 = _make_revision(db, install.owner_id, [_spec("acme", "pdf"), _spec("acme", "xls")])
    plugin_sync.merge(db, install, rev2)

    assert {l.snapshot_plugin_name for l in _bundle_links(db, install.id)} == {"pdf", "xls"}


def test_merge_never_touches_marketplace_links(db: Session) -> None:
    """A consumer's own source=marketplace link survives a bundle merge."""
    install = _make_install(db, _make_user(db))
    # Consumer marketplace link (plugin_id None is fine for this isolated test).
    mkt_link = AgentPluginLink(
        agent_id=install.id,
        plugin_id=None,
        source=PluginSource.marketplace,
        snapshot_marketplace_name=None,
        installed_version="9.9",
    )
    db.add(mkt_link)
    db.commit()
    mkt_id = mkt_link.id

    rev = _make_revision(db, install.owner_id, [_spec("acme", "pdf")])
    plugin_sync.merge(db, install, rev)

    # Marketplace link untouched; bundle link created.
    surviving = db.get(AgentPluginLink, mkt_id)
    assert surviving is not None
    assert surviving.source == PluginSource.marketplace
    assert {l.snapshot_plugin_name for l in _bundle_links(db, install.id)} == {"pdf"}


def test_collision_with_marketplace_plugin_is_skipped(db: Session) -> None:
    """A bundle plugin colliding with a same-named marketplace plugin is skipped."""
    owner_id = _make_user(db)
    install = _make_install(db, owner_id)
    # Real marketplace + plugin rows so the marketplace link's on-disk dir key
    # (marketplace.name, plugin.name) = (acme, pdf) resolves via link.plugin.
    marketplace = LLMPluginMarketplace(
        name="acme", url="https://example.com/acme.git", user_id=owner_id
    )
    db.add(marketplace)
    db.commit()
    db.refresh(marketplace)
    plugin = LLMPluginMarketplacePlugin(name="pdf", marketplace_id=marketplace.id)
    db.add(plugin)
    db.commit()
    db.refresh(plugin)

    mkt_link = AgentPluginLink(
        agent_id=install.id,
        plugin_id=plugin.id,
        source=PluginSource.marketplace,
    )
    db.add(mkt_link)
    db.commit()

    rev = _make_revision(db, install.owner_id, [_spec("acme", "pdf"), _spec("acme", "other")])
    created = plugin_sync.materialise(db, install, rev)
    db.commit()

    # pdf collides → skipped; other is created.
    assert created == 1
    assert {l.snapshot_plugin_name for l in _bundle_links(db, install.id)} == {"other"}


# ── Public SSH-URL normalization (keyless container clone) ────────────────────


def test_manifest_rewrites_public_ssh_marketplace_url_to_https(db: Session) -> None:
    """A git@github.com: marketplace URL is rewritten to HTTPS in the manifest.

    The container has no GitHub SSH key, so a public marketplace configured with
    an SSH-form URL must clone over HTTPS or the plugin fails to install.
    """
    from app.services.plugins.llm_plugin_service import LLMPluginService

    owner_id = _make_user(db)
    install = _make_install(db, owner_id)

    marketplace = LLMPluginMarketplace(
        name="claude-plugins-official",
        url="git@github.com:anthropics/claude-plugins-official.git",
        user_id=owner_id,
    )
    db.add(marketplace)
    db.commit()
    db.refresh(marketplace)
    plugin = LLMPluginMarketplacePlugin(
        name="frontend-design",
        marketplace_id=marketplace.id,
        source_path="plugins/frontend-design",
        commit_hash="f1be96f0",
    )
    db.add(plugin)
    db.commit()
    db.refresh(plugin)
    db.add(AgentPluginLink(
        agent_id=install.id, plugin_id=plugin.id, source=PluginSource.marketplace,
        installed_commit_hash="f1be96f0",
    ))
    db.commit()

    manifest = LLMPluginService.build_plugin_manifest(db, install.id)
    entries = [e for e in manifest["plugins"] if e["plugin_name"] == "frontend-design"]
    assert len(entries) == 1
    git = entries[0]["git"]
    assert git["url"] == "https://github.com/anthropics/claude-plugins-official.git"
    assert git["subdir"] == "plugins/frontend-design"
    assert git["ref"] == "f1be96f0"


def test_manifest_leaves_unknown_ssh_host_untouched(db: Session) -> None:
    """A genuinely-private SSH host is NOT rewritten (private flow preserved)."""
    from app.services.plugins.llm_plugin_service import LLMPluginService

    owner_id = _make_user(db)
    install = _make_install(db, owner_id)

    private_url = "git@git.internal.corp:team/private-plugins.git"
    marketplace = LLMPluginMarketplace(
        name="private", url=private_url, user_id=owner_id,
    )
    db.add(marketplace)
    db.commit()
    db.refresh(marketplace)
    plugin = LLMPluginMarketplacePlugin(
        name="secret", marketplace_id=marketplace.id, source_path="p/secret",
        commit_hash="deadbeef",
    )
    db.add(plugin)
    db.commit()
    db.refresh(plugin)
    db.add(AgentPluginLink(
        agent_id=install.id, plugin_id=plugin.id, source=PluginSource.marketplace,
        installed_commit_hash="deadbeef",
    ))
    db.commit()

    manifest = LLMPluginService.build_plugin_manifest(db, install.id)
    entries = [e for e in manifest["plugins"] if e["plugin_name"] == "secret"]
    assert len(entries) == 1
    # Unknown host left verbatim — the SSH-key-injection path handles it later.
    assert entries[0]["git"]["url"] == private_url


def test_normalize_public_git_url_unit() -> None:
    """Direct coverage of the backend normalizer across URL forms."""
    from app.services.plugins.llm_plugin_service import LLMPluginService as S

    n = S._normalize_public_git_url
    assert n("git@github.com:anthropics/x.git") == "https://github.com/anthropics/x.git"
    assert n("git@github.com:anthropics/x") == "https://github.com/anthropics/x.git"
    assert n("ssh://git@gitlab.com/org/x.git") == "https://gitlab.com/org/x.git"
    assert n("ssh://git@bitbucket.org/org/x") == "https://bitbucket.org/org/x.git"
    # Passthrough: already HTTPS, git protocol, unknown/private host, None.
    assert n("https://github.com/org/x.git") == "https://github.com/org/x.git"
    assert n("git://github.com/org/x.git") == "git://github.com/org/x.git"
    assert n("git@my-host.internal:org/x.git") == "git@my-host.internal:org/x.git"
    assert n(None) is None


# ── url-plugin ref resolution (external repo, not marketplace commit) ──────────


def _make_url_plugin_link(
    db: Session, owner_id: uuid.UUID, *,
    source_url: str = "https://github.com/getsentry/sentry-for-claude.git",
    source_branch: str = "main",
    source_commit_hash: str | None = None,
    commit_hash: str | None = "1b46aa6d",  # MARKETPLACE repo commit (the trap)
) -> tuple[Agent, "object"]:
    from app.models.plugins.llm_plugin import PluginSourceType

    install = _make_install(db, owner_id)
    marketplace = LLMPluginMarketplace(
        name="acme", url="https://github.com/acme/marketplace.git", user_id=owner_id,
    )
    db.add(marketplace)
    db.commit()
    db.refresh(marketplace)
    plugin = LLMPluginMarketplacePlugin(
        name="sentry",
        marketplace_id=marketplace.id,
        source_type=PluginSourceType.url,
        source_url=source_url,
        source_branch=source_branch,
        source_commit_hash=source_commit_hash,
        commit_hash=commit_hash,
    )
    db.add(plugin)
    db.commit()
    db.refresh(plugin)
    db.add(AgentPluginLink(
        agent_id=install.id, plugin_id=plugin.id, source=PluginSource.marketplace,
        # url plugins also store the marketplace commit here (cosmetic — the url
        # branch of the resolver doesn't read it).
        installed_commit_hash=commit_hash,
    ))
    db.commit()
    return install, plugin


def test_url_plugin_ref_falls_back_to_branch_not_marketplace_commit(db: Session) -> None:
    """url plugin with no source_commit_hash → ref=source_branch, NOT commit_hash.

    Regression for the sentry bug: commit_hash is the marketplace repo's HEAD and
    does not exist in the external source_url repo (git checkout → unable to read
    tree). The resolver must fall back to the external branch.
    """
    from app.services.plugins.llm_plugin_service import LLMPluginService

    owner_id = _make_user(db)
    install, _ = _make_url_plugin_link(
        db, owner_id, source_commit_hash=None, commit_hash="1b46aa6d",
    )
    manifest = LLMPluginService.build_plugin_manifest(db, install.id)
    entry = next(e for e in manifest["plugins"] if e["plugin_name"] == "sentry")
    git = entry["git"]
    assert git["url"] == "https://github.com/getsentry/sentry-for-claude.git"
    assert git["subdir"] == ""
    assert git["ref"] == "main", f"expected branch fallback, got {git['ref']}"
    assert git["ref"] != "1b46aa6d", "must not use the marketplace commit"


def test_url_plugin_ref_pins_to_source_commit_when_present(db: Session) -> None:
    """url plugin WITH source_commit_hash → ref pins to that external commit."""
    from app.services.plugins.llm_plugin_service import LLMPluginService

    owner_id = _make_user(db)
    install, _ = _make_url_plugin_link(
        db, owner_id, source_commit_hash="extREALsha", commit_hash="1b46aa6d",
    )
    manifest = LLMPluginService.build_plugin_manifest(db, install.id)
    entry = next(e for e in manifest["plugins"] if e["plugin_name"] == "sentry")
    assert entry["git"]["ref"] == "extREALsha"
