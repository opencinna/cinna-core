"""Plugin snapshot + sync helpers for bundle propagation.

A publisher ships their installed plugins (``AgentPluginLink`` rows) as part of
a bundle revision (``revision.plugin_specs``) plus the plugin *files* inside the
revision workspace snapshot (``plugins/<mkt>/<plugin>/``). On install the
consumer receives ``source=bundle`` plugin links pre-populated with the
published per-mode + disabled flags and the frozen ``plugin.json`` config; on
apply-update the consumer's bundle links are merged so a user's enable/disable +
per-mode toggles survive a behaviorally-unchanged plugin while changed / added /
removed plugins are synced from the new revision.

Identity ("same plugin" across revisions) uses the **behavioral signature**
``(snapshot_marketplace_name, snapshot_plugin_name)`` — the on-disk dir segments
that also key the manifest entry. ``version`` / ``commit_hash`` / ``config`` are
refreshed on a behaviorally-unchanged match but are NOT part of identity.

Only ``source=bundle`` links are ever touched here. A consumer's own
``source=marketplace`` plugins are independent and never reconciled by bundle
apply-update (mixed sources coexist).
"""
import logging
import uuid

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.plugins.llm_plugin import AgentPluginLink, PluginSource

logger = logging.getLogger(__name__)


def snapshot_plugin_specs(links: list[AgentPluginLink]) -> list[dict]:
    """Project ``AgentPluginLink`` rows into the revision snapshot shape.

    Returns a list of ``{marketplace_name, plugin_name, version, commit_hash,
    conversation_mode, building_mode, disabled, config, snapshot_subdir}`` dicts.
    Works on both marketplace- and bundle-sourced publisher links: identity +
    display name resolve from the live plugin row (marketplace) or the frozen
    snapshot fields (bundle).
    """
    specs: list[dict] = []
    for link in links:
        marketplace_name, plugin_name, config = _resolve_link_identity(link)
        if not (marketplace_name and plugin_name):
            logger.warning(
                "Skipping plugin link %s in snapshot — unresolvable identity",
                link.id,
            )
            continue
        specs.append(
            {
                "marketplace_name": marketplace_name,
                "plugin_name": plugin_name,
                "version": link.installed_version,
                "commit_hash": link.installed_commit_hash,
                "conversation_mode": bool(link.conversation_mode),
                "building_mode": bool(link.building_mode),
                "disabled": bool(link.disabled),
                "config": config,
                "snapshot_subdir": f"plugins/{marketplace_name}/{plugin_name}",
            }
        )
    return specs


def _resolve_link_identity(
    link: AgentPluginLink,
) -> tuple[str | None, str | None, dict | None]:
    """Resolve (marketplace_name, plugin_name, config) for a publisher link.

    Marketplace links resolve from the live ``plugin`` + ``marketplace`` rows;
    bundle links use the frozen snapshot fields. The frozen ``config`` is the
    plugin's ``plugin.json`` (used for consumer-side UI display).
    """
    if link.source == PluginSource.bundle:
        return (
            link.snapshot_marketplace_name,
            link.snapshot_plugin_name,
            link.snapshot_config,
        )
    plugin = link.plugin
    if plugin is None:
        return (None, None, None)
    marketplace = plugin.marketplace
    marketplace_name = marketplace.name if marketplace else None
    return (marketplace_name, plugin.name, plugin.config)


def sig(source: object) -> tuple:
    """Return the behavioral signature of a plugin link or snapshot dict.

    Signature = ``(snapshot_marketplace_name, snapshot_plugin_name)``. Works on
    both an :class:`AgentPluginLink` row (bundle-sourced) and a snapshot
    ``dict`` so the merge can compare existing rows against new revision specs
    uniformly.
    """
    if isinstance(source, dict):
        return (
            source.get("marketplace_name"),
            source.get("plugin_name"),
        )
    return (
        getattr(source, "snapshot_marketplace_name", None),
        getattr(source, "snapshot_plugin_name", None),
    )


def _create_from_spec(
    session: Session, install: Agent, spec: dict
) -> AgentPluginLink:
    """Create one ``source=bundle`` ``AgentPluginLink`` from a revision spec.

    ``plugin_id`` is NULL (no marketplace row to resolve against); identity +
    display come from the snapshot fields. Per-mode + disabled flags use the
    published state.
    """
    link = AgentPluginLink(
        agent_id=install.id,
        plugin_id=None,
        source=PluginSource.bundle,
        snapshot_marketplace_name=spec.get("marketplace_name"),
        snapshot_plugin_name=spec.get("plugin_name"),
        snapshot_config=spec.get("config"),
        installed_version=spec.get("version"),
        installed_commit_hash=spec.get("commit_hash"),
        conversation_mode=bool(spec.get("conversation_mode", True)),
        building_mode=bool(spec.get("building_mode", True)),
        disabled=bool(spec.get("disabled", False)),
    )
    session.add(link)
    return link


def _group_by_sig(specs: list) -> dict[tuple, dict]:
    """Group revision plugin specs by behavioral signature.

    Duplicate signatures within one revision are assumed unique; on a collision
    the later spec wins (documented limitation). Non-dict entries are skipped.
    """
    grouped: dict[tuple, dict] = {}
    for spec in specs or []:
        if not isinstance(spec, dict):
            continue
        grouped[sig(spec)] = spec
    return grouped


def _bundle_links(session: Session, install: Agent) -> list[AgentPluginLink]:
    """Return the install's ``source=bundle`` plugin links only.

    Marketplace-sourced links are the consumer's own and are never touched by
    bundle propagation.
    """
    return list(
        session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.agent_id == install.id,
                AgentPluginLink.source == PluginSource.bundle,
            )
        ).all()
    )


def _marketplace_dir_keys(session: Session, install: Agent) -> set[tuple]:
    """On-disk dir keys (mkt_name, plugin_name) of the install's marketplace links.

    Used to detect a collision between a bundle plugin and a same-named
    consumer ``source=marketplace`` plugin (§6.4): both would resolve to the
    same ``/app/workspace/plugins/<mkt>/<plugin>/`` directory. Default policy is
    original names + collision check — a colliding bundle plugin is skipped and
    logged rather than silently overwriting the consumer's own plugin.
    """
    keys: set[tuple] = set()
    rows = session.exec(
        select(AgentPluginLink).where(
            AgentPluginLink.agent_id == install.id,
            AgentPluginLink.source == PluginSource.marketplace,
        )
    ).all()
    for link in rows:
        marketplace_name, plugin_name, _config = _resolve_link_identity(link)
        if marketplace_name and plugin_name:
            keys.add((marketplace_name, plugin_name))
    return keys


def materialise(
    session: Session, install: Agent, revision: AgentBundleRevision
) -> int:
    """Create ``source=bundle`` plugin links on ``install`` from the revision.

    Used at install time. Creates one link per ``revision.plugin_specs`` entry
    with the published flags + frozen config. Returns the number created.

    Caller owns the commit — this only stages rows (mirrors the create branch
    of :func:`merge`).
    """
    collisions = _marketplace_dir_keys(session, install)
    created = 0
    for spec in revision.plugin_specs or []:
        if not isinstance(spec, dict):
            continue
        key = sig(spec)
        if key in collisions:
            logger.warning(
                "Skipping bundle plugin %s/%s for install %s — collides with an "
                "existing source=marketplace plugin of the same name",
                key[0], key[1], install.id,
            )
            continue
        _create_from_spec(session, install, spec)
        created += 1
    return created


def merge(
    session: Session, install: Agent, revision: AgentBundleRevision
) -> None:
    """Reconcile ``install``'s bundle plugin links against the new revision.

    Algorithm (only ``source=bundle`` links are touched — the consumer's own
    ``source=marketplace`` links are never reconciled):

    - For each existing bundle link whose signature is still present in the new
      revision: keep the row (preserve the user's ``disabled`` + per-mode
      toggles) but refresh the frozen ``config`` / ``version`` / ``commit_hash``
      from the new spec. That spec is then consumed.
    - Existing bundle links whose signature is gone (changed or removed by the
      publisher) are deleted.
    - Remaining (unconsumed) specs are added / changed plugins → create new
      ``source=bundle`` links with the published flags.

    Commits at the end.
    """
    new_by_sig = _group_by_sig(revision.plugin_specs)
    collisions = _marketplace_dir_keys(session, install)

    existing = _bundle_links(session, install)

    for row in existing:
        signature = sig(row)
        spec = new_by_sig.pop(signature, None)
        if spec is not None:
            # Behaviorally unchanged — keep the row (and its toggles), refresh
            # only the frozen metadata so the UI shows the new version/config.
            changed = False
            new_version = spec.get("version")
            if new_version is not None and row.installed_version != new_version:
                row.installed_version = new_version
                changed = True
            new_commit = spec.get("commit_hash")
            if new_commit is not None and row.installed_commit_hash != new_commit:
                row.installed_commit_hash = new_commit
                changed = True
            new_config = spec.get("config")
            if new_config is not None and row.snapshot_config != new_config:
                row.snapshot_config = new_config
                changed = True
            if changed:
                session.add(row)
        else:
            # Signature gone from the new revision → publisher changed or
            # removed this plugin. Delete it (bundle-owned link).
            session.delete(row)

    # Remaining specs are new or behaviorally-changed plugins.
    for spec in new_by_sig.values():
        key = sig(spec)
        if key in collisions:
            logger.warning(
                "Skipping bundle plugin %s/%s for install %s on apply-update — "
                "collides with an existing source=marketplace plugin",
                key[0], key[1], install.id,
            )
            continue
        _create_from_spec(session, install, spec)

    session.commit()
