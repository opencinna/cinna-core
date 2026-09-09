/**
 * Shared rendering rules for the agent addons projection.
 *
 * `GET /agents/{id}/addons` folds the plugin links and the environment's skill
 * index into **one** list (plan §2), so the client half of that fold has to be
 * shared too: the card, its "Show all" Sheet and the detail dialog must agree
 * on what a dot means, what a source is called and which caches a mutation
 * touches. A second copy of any of the three is how one list starts reading as
 * two again — which is the seam this projection exists to remove.
 *
 * Nothing here matches on prose. The server gives every flagged condition a
 * stable `code` plus a human `message`; the tone comes from the code and the
 * sentence is printed as written.
 */
import type { QueryClient } from "@tanstack/react-query"
import {
  FolderCode,
  GraduationCap,
  type LucideIcon,
  Package,
  Store,
} from "lucide-react"

import type {
  AddonPublic,
  LLMPluginMarketplacePluginPublic,
  SkillPackageEntry,
} from "@/client"
import type { RowStatus } from "@/components/Common/ListRow"
import {
  type MarketplaceFormat,
  unsupportedReasonSentence,
} from "@/utils/marketplace"
import {
  skillPackageVersionLabel,
  skillPublisherLabel,
} from "@/utils/skillCatalog"

/** The query key the Addons tab reads. */
export function addonsQueryKey(agentId: string): readonly unknown[] {
  return ["agent", agentId, "addons"]
}

/**
 * Everything an install-set change touches, in one call.
 *
 * The projection is a *join*, so a mutation on either half invalidates it —
 * and the two halves still have their own consumers (the command popup reads
 * the skill index, `AddSkillToAgentDialog` reads the plugin list), which is
 * why this invalidates all three rather than replacing them. Written once
 * because three call sites that each remember two of the three keys is exactly
 * how a card starts naming a set it no longer has.
 *
 * `catalog` additionally refreshes the skills catalog's own
 * `installed_in_agent_ids` / `install_count` — the counters the install and
 * uninstall paths move in opposite directions.
 */
export function invalidateAddons(
  queryClient: QueryClient,
  agentId: string,
  options?: { catalog?: boolean },
): void {
  queryClient.invalidateQueries({ queryKey: addonsQueryKey(agentId) })
  queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })
  queryClient.invalidateQueries({ queryKey: ["agent", agentId, "skills"] })
  if (options?.catalog) {
    queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
  }
}

/**
 * Sentences for the two failures that belong to the *link*, not to a skill.
 *
 * `orphan` deliberately does not name a marketplace: the service also emits a
 * degenerate orphan for an index entry with no `plugin_ref` at all, whose
 * `marketplace_name` is null, so the copy has to read as "the engine loads
 * this, nothing installed it" for both shapes.
 *
 * `source_unavailable` covers two causes, because the server gives them one
 * code (`AddonsService._source_unavailable`): the package, revision or
 * marketplace was deleted, **or** a re-sync re-derived `supported=false` onto
 * an entry this agent already has — upstream moved it to npm, dropped its
 * SKILL.md, or wrote a path that escapes the repo. The sentence therefore
 * cannot assert a disappearance the user can go and disprove; it names the
 * consequence both causes share and leaves "uninstall is the only verb left"
 * as the implication. If the server ever splits the code, this narrows back to
 * the removal half and the five sentences in `utils/marketplace.ts` carry the
 * other.
 */
const LINK_STATUS_COPY: Record<string, string> = {
  orphan:
    "Loaded by the engine but not installed — the next environment sync removes it.",
  source_unavailable:
    "Its source can no longer deliver files — it was removed, or this platform can no longer install it.",
}

/** The sentence a skill contributed to the row's status, if one did. */
function skillStatusMessage(addon: AddonPublic, code: string): string | null {
  for (const skill of addon.skills ?? []) {
    if (skill.error && (skill.error.code === code || !code)) {
      return skill.error.message || null
    }
  }
  for (const skill of addon.skills ?? []) {
    if (skill.warning && (skill.warning.code === code || !code)) {
      return skill.warning.message || null
    }
  }
  return null
}

/**
 * The row's leading dot — one resolution, never re-derived per call site.
 *
 * Precedence: a broken or flagged addon outranks a disabled one, because
 * "disabled" is a choice the user made and "broken" is not. The disabled fact
 * is not lost when a warning outranks it: the row is still `muted`.
 *
 * Rendered unconditionally, healthy rows included (memory:
 * `project_ui_spec_agent_skills`) — `ListRow` draws the dot only when it is
 * given one, so "ok means no dot" would indent every clean row differently
 * from every flagged one.
 */
export function addonRowStatus(addon: AddonPublic): RowStatus {
  const status = addon.status ?? "ok"
  const code = addon.status_code ?? ""

  if (status === "error" || status === "warning") {
    const tone = status === "error" ? "error" : "warning"
    const label =
      LINK_STATUS_COPY[code] ??
      skillStatusMessage(addon, code) ??
      (tone === "error"
        ? "The engine cannot load this."
        : "This works, but something is flagged.")
    return { tone, label }
  }

  if (addon.link?.disabled) {
    return { tone: "off", label: "Disabled" }
  }
  return { tone: "on", label: "Loaded — the engine can see this" }
}

/**
 * Is this row carrying a problem worth a panel?
 *
 * The predicate lives here rather than as an inline `addon.status !== "ok"` at
 * each call site because `status` is optional on the wire (`status?: string`):
 * a row that arrived without one would fail that test and be rendered as
 * flagged, carrying the *healthy* sentence `addonRowStatus` hands back. One
 * home for the default, one for the test.
 */
export function addonIsFlagged(addon: AddonPublic): boolean {
  return (addon.status ?? "ok") !== "ok"
}

/** Where a row came from, as the one glyph a mixed list can afford. */
export interface AddonSourceFlag {
  icon: LucideIcon
  label: string
}

/**
 * The source flag, one per row.
 *
 * A local skill gets a glyph here too — unlike the old Skills card, where
 * local was the unmarked default. In a list that mixes four origins nothing is
 * the default, and a row with no flag would read as "we forgot" rather than as
 * "this one is ours". Copy verbatim from plan §10.
 */
export function addonSourceFlag(addon: AddonPublic): AddonSourceFlag {
  switch (addon.source) {
    case "bundle":
      return {
        icon: Package,
        label: "Delivered by the bundle — managed by its publisher",
      }
    case "catalog":
      return {
        icon: GraduationCap,
        label: "Installed from the skills catalog",
      }
    case "local":
      return { icon: FolderCode, label: "Built in this agent" }
    default:
      return {
        icon: Store,
        label: addon.marketplace_name
          ? `From the marketplace ${addon.marketplace_name}`
          : "From a marketplace",
      }
  }
}

/**
 * The marketplace format, in the words plan §10 chose.
 *
 * A fact rather than a badge: `version` already spends the row's one badge,
 * and a format is read once when the user wonders what a row is, never scanned
 * for down a list. Only marketplace rows carry a `plugin_type`, and since the
 * Codex and skills-repository parsers shipped all three keys have data — a
 * `codex` or `skills` row now reads its own word rather than borrowing
 * "Claude plugin".
 *
 * Singular on purpose: this is one entry inside a marketplace, where
 * `MARKETPLACE_FORMATS` in `utils/marketplace.ts` names the *repository* it
 * came from ("Codex plugins", "Skills repository").
 */
const FORMAT_LABEL = {
  claude: "Claude plugin",
  codex: "Codex plugin",
  skills: "Skill",
} as const satisfies Record<MarketplaceFormat, string>

export function addonFormatLabel(
  pluginType: string | null | undefined,
): string | null {
  // Keyed by the generated union, then read through a bare `string`: the wire
  // field is `plugin_type: string`, so the *read* has to degrade even though
  // the table above cannot. Null rather than the raw code — the format is one
  // fact among several on a row, and printing `skills` at a user says less than
  // saying nothing.
  if (!pluginType) return null
  return pluginType in FORMAT_LABEL
    ? FORMAT_LABEL[pluginType as MarketplaceFormat]
    : null
}

/** What a row is: the two words plan §10 allows a row to use. */
export type AddonEntryKind = "plugin" | "skill"

/**
 * Which of those two words a marketplace format belongs to.
 *
 * A `skills`-format entry *is* a skill: the same entry, once installed, prints
 * "Skill" as its format fact, so offering it as a generic plugin in the Add
 * addon dialog would give one entry two vocabularies — the seam plan §10's
 * source/format rules exist to close.
 *
 * A `Record` keyed by the generated union for the same reason `FORMAT_LABEL`
 * is one: a fourth server-side format is a missing key here and fails to
 * compile, rather than silently defaulting a new kind of thing to "plugin".
 */
const FORMAT_KIND = {
  claude: "plugin",
  codex: "plugin",
  skills: "skill",
} as const satisfies Record<MarketplaceFormat, AddonEntryKind>

/**
 * The word a marketplace entry of this format goes by.
 *
 * Absent or unrecognised degrades to "plugin": that is what every marketplace
 * entry was before formats existed, and it is the kind the addons projection
 * gives the same entry once it is installed
 * (`AddonsService._link_row`), so the two halves still agree on a format this
 * build has not heard of.
 */
export function addonFormatKind(
  pluginType: string | null | undefined,
): AddonEntryKind {
  if (pluginType && pluginType in FORMAT_KIND) {
    return FORMAT_KIND[pluginType as MarketplaceFormat]
  }
  return "plugin"
}

/**
 * What a row is called in prose — plan §10: rows say plugin or skill, never
 * "addon". The umbrella lives on the tab, not on the row.
 */
export function addonNoun(addon: AddonPublic): string {
  return addon.kind === "skill" ? "skill" : "plugin"
}

/**
 * One installable thing, normalised across the two sources the Add addon
 * dialog searches.
 *
 * Plugins come from `discoverPlugins` and skills from `listSkillCatalog`; a row
 * that rendered each source's own shape would make one result list look like
 * two, which is the seam this projection removed on the other side of the
 * install.
 */
export interface AddAddonResult {
  /** Unique across both sources: the source's own id, prefixed by source. */
  key: string
  /**
   * What the row *says* it is, and which segment of the kind filter it falls
   * under — derived from the marketplace format, never from the transport that
   * delivers it. A `skills`-format marketplace entry is a skill here because it
   * is a skill once installed (plan §10).
   */
  kind: AddonEntryKind
  /**
   * Where it comes from, and therefore how it is installed: a marketplace entry
   * goes through `installAgentPlugin` and a catalog package through
   * `installAgentSkill`. Separate from `kind` precisely because the two stopped
   * being one field the moment a marketplace could publish skills — branching
   * the install on the word would post a package id to the plugin endpoint.
   */
  source: "marketplace" | "catalog"
  /** `plugin_id` for a marketplace entry, the package's uuid for a catalog one. */
  id: string
  name: string
  description: string | null
  version: string | null
  /** Where it comes from: the marketplace for a plugin, the catalog for a skill. */
  origin: string | null
  /**
   * Who made it — the plugin manifest's author, the package's publisher — as
   * the row's second badge. Null when the source did not say.
   */
  author: string | null
  /** Everything true of the result that nobody scans a result list by. */
  facts: Array<string | false | null | undefined>
  /**
   * The entry as its source returned it, for the row's Details dialog. Exactly
   * one is set, by `source`; kept whole rather than flattened because the
   * dialog reads a dozen fields the row never will.
   */
  plugin?: LLMPluginMarketplacePluginPublic
  package?: SkillPackageEntry
  /**
   * This platform can install it. False only for a marketplace entry the
   * syncer refused — of any format, skills included:
   * `install_plugin_for_agent` answers those with `409 plugin_unsupported`, so
   * an enabled row here would be a button that cannot work. Catalog packages
   * are always installable — they were published from a workspace this platform
   * already ran.
   */
  supported: boolean
  /**
   * Why not, as a sentence. Present exactly when `supported` is false, and
   * rendered as the row's *first* fact: the answer to "why can I not pick
   * this" has to arrive before the description.
   */
  unsupportedReason: string | null
}

/**
 * This result is in the list to be read, not to be picked: this platform
 * cannot install it. Exported because the cap has to be able to tell the two
 * groups apart — capping a list that has just sorted the blocked rows to the
 * bottom would delete exactly the rows both this module and
 * `AddAddonResultRow` promise never to hide.
 */
export function isAddonResultBlocked(result: AddAddonResult): boolean {
  return !result.supported
}

/** Which kinds the dialog's filter is currently letting through. */
export type AddAddonKind = "all" | "plugin" | "skill"

interface BuildAddonResultsInput {
  plugins: LLMPluginMarketplacePluginPublic[]
  packages: SkillPackageEntry[]
  /** What the agent already carries, from the addons projection. */
  installedAddons: AddonPublic[]
  agentId: string
  /** The debounced search box, already trimmed. */
  query: string
  kind: AddAddonKind
}

/**
 * Fold the two search sources into one ordered result list.
 *
 * A pure function, deliberately: it is the piece that carries every rule about
 * what a result *is*, and inside the dialog it could only be exercised by
 * rendering the dialog.
 *
 * Two asymmetries it hides, both forced by the APIs: plugin discovery is
 * searched server-side while the skills catalog takes no query at all and is
 * filtered here — over the same four fields, so switching the kind filter does
 * not silently change what "matching" means.
 *
 * The kind filter stays client-side (spec S3): it spans two sources *and* two
 * formats within one of them, so `discoverPlugins({ pluginType })` could only
 * ever filter one third of the list and would still leave this fold deciding
 * the rest.
 */
export function buildAddonResults({
  plugins,
  packages,
  installedAddons,
  agentId,
  query,
  kind,
}: BuildAddonResultsInput): AddAddonResult[] {
  // What this agent already carries — left out of the list. Plugin links carry
  // the marketplace plugin's id; a catalog install carries the package's. An
  // installed entry is a row on the Addons card already, so offering it again
  // here, even muted, is a second row for the same thing.
  const installedPluginIds = new Set(
    installedAddons
      .map((addon) => addon.link?.plugin_id)
      .filter((id): id is string => !!id),
  )
  const installedPackageIds = new Set(
    installedAddons
      .map((addon) => addon.link?.skill_package_id)
      .filter((id): id is string => !!id),
  )

  const rows: AddAddonResult[] = []

  // Every marketplace entry is considered whatever the filter says, because the
  // format decides the word: the Skills segment has to reach a `skills`-format
  // entry, and the Plugins segment must not. Filtering the *loop* by kind — as
  // this did while every discover row was a plugin by construction — is what
  // hid those entries from the segment that names them.
  for (const plugin of plugins) {
    const entryKind = addonFormatKind(plugin.plugin_type)
    if (kind !== "all" && kind !== entryKind) continue
    if (installedPluginIds.has(plugin.id)) continue
    // Optional on the wire, so an entry synced before the flag existed reads
    // as installable rather than as refused.
    const supported = plugin.supported !== false
    rows.push({
      key: `marketplace:${plugin.id}`,
      kind: entryKind,
      source: "marketplace",
      id: plugin.id,
      name: plugin.name,
      description: plugin.description,
      version: plugin.version,
      origin: plugin.marketplace_name
        ? `From the marketplace ${plugin.marketplace_name}`
        : null,
      // Manifest author, else the marketplace owner — the projection makes
      // the same choice for the installed row, so the badge does not change
      // the moment the entry is installed.
      author:
        plugin.author_name ||
        plugin.author_email ||
        plugin.marketplace_owner ||
        null,
      facts: [
        // The same word the installed row prints for the same entry, from the
        // same table — which is the whole point of deriving the kind from it.
        addonFormatLabel(plugin.plugin_type),
        plugin.category && `Category ${plugin.category}`,
      ],
      plugin,
      supported,
      unsupportedReason: supported
        ? null
        : unsupportedReasonSentence(plugin.unsupported_reason),
    })
  }

  if (kind !== "plugin") {
    const needle = query.toLowerCase()
    for (const pkg of packages) {
      const haystack = [
        pkg.name,
        pkg.display_name,
        pkg.description ?? "",
        skillPublisherLabel(pkg),
      ]
        .join(" ")
        .toLowerCase()
      if (needle && !haystack.includes(needle)) continue
      if (
        installedPackageIds.has(pkg.id) ||
        (pkg.installed_in_agent_ids ?? []).includes(agentId)
      ) {
        continue
      }
      rows.push({
        key: `catalog:${pkg.id}`,
        kind: "skill",
        source: "catalog",
        id: pkg.id,
        name: pkg.display_name,
        description: pkg.description ?? null,
        version: skillPackageVersionLabel(pkg)?.replace(/^v/, "") ?? null,
        origin: "From the skills catalog",
        author: skillPublisherLabel(pkg),
        facts: [
          // People, not installs: the server counts one per user, so "6
          // installs" would over-report a single consumer with six agents.
          (pkg.install_count ?? 0) > 0 &&
            `Used by ${pkg.install_count} ${
              pkg.install_count === 1 ? "person" : "people"
            }`,
        ],
        package: pkg,
        supported: true,
        unsupportedReason: null,
      })
    }
  }

  // What you can act on first. Unsupported rows stay in the list — hiding one
  // would make an admin's half-broken marketplace look empty (plan §10) — but
  // they are not selectable, so a list that opened on them would look like a
  // list of nothing to do.
  rows.sort((a, b) => {
    const aBlocked = isAddonResultBlocked(a)
    const bBlocked = isAddonResultBlocked(b)
    if (aBlocked !== bBlocked) return aBlocked ? 1 : -1
    return a.name.localeCompare(b.name)
  })
  return rows
}
