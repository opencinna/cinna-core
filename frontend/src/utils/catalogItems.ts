import { Bot, GraduationCap, Laptop, type LucideIcon } from "lucide-react"

import type {
  CatalogItem,
  CatalogLatest,
  CatalogRevision,
  SkillCredentialRequirementPublic,
} from "@/client"
import { credentialTypeLabel } from "@/utils/skillCredentials"

/**
 * The words and glyphs the unified catalog uses for its three kinds of item,
 * in one module so the tile, the detail route, the Sheets and the admin table
 * cannot spell them two ways (U12; guidelines §2 "The same entity, before and
 * after"). No git words live here or anywhere the catalog renders.
 */
export type CatalogKind = "cloud" | "desktop" | "skill"

export const CATALOG_KINDS: Record<
  CatalogKind,
  {
    /** The kind word on a tile badge and in the admin table's Runs column. */
    word: string
    /** The segment label on the page toolbar. */
    segment: string
    icon: LucideIcon
  }
> = {
  cloud: { word: "Runs in Cinna", segment: "Agents in Cinna", icon: Bot },
  desktop: {
    word: "Runs on your computer",
    segment: "Agents on your computer",
    icon: Laptop,
  },
  skill: { word: "Skill", segment: "Skills", icon: GraduationCap },
}

/** Which of the three kinds an item is. A bundle without `runs_on` is cloud. */
export function catalogKindOf(
  item: Pick<CatalogItem, "kind" | "runs_on">,
): CatalogKind {
  if (item.kind === "skill") return "skill"
  return item.runs_on === "desktop" ? "desktop" : "cloud"
}

/**
 * "v1.3" when the publisher named a version, "Version 4" when they did not.
 * `||`, not `??`: an absent version arrives as `""` as often as `null`.
 */
export function catalogVersionLabel(
  latest: Pick<CatalogLatest | CatalogRevision, "version" | "revision_number">,
): string {
  return latest.version
    ? `v${latest.version}`
    : `Version ${latest.revision_number}`
}

export type CatalogVisibility = "private" | "users" | "public"

/** General access, as the People dialog offers it and the facts name it. */
export const CATALOG_VISIBILITY_OPTIONS: {
  value: CatalogVisibility
  label: string
}[] = [
  { value: "public", label: "Anyone signed in" },
  { value: "users", label: "Only the people and roles below" },
  { value: "private", label: "Private" },
]

const VISIBILITY_FACT: Record<CatalogVisibility, string> = {
  public: "Anyone signed in",
  users: "People and roles named",
  private: "Private",
}

/** The Access fact / "Who can get it" cell; a delisting rides on it. */
export function catalogAccessFact(
  item: Pick<CatalogItem, "visibility" | "is_listed">,
): string {
  const base =
    VISIBILITY_FACT[item.visibility as CatalogVisibility] || item.visibility
  return item.is_listed ? base : `${base}, delisted`
}

export function isCatalogVisibility(value: string): value is CatalogVisibility {
  return value === "private" || value === "users" || value === "public"
}

/** "3 people", "1 person" — a count always carries its unit (§2 Fact labels). */
export function peopleCount(n: number): string {
  return `${n} ${n === 1 ? "person" : "people"}`
}

/**
 * One `required_credential_specs` entry, narrowed to what the catalog shows.
 * The generated type is a loose dict (the spec schema is server-side,
 * `credential_spec.build_spec`), so every key is read defensively.
 */
export function catalogCredentialRequirements(
  specs: CatalogItem["required_credential_specs"],
): SkillCredentialRequirementPublic[] {
  if (!Array.isArray(specs)) return []
  return specs.flatMap((spec) => {
    const name = typeof spec.name === "string" ? spec.name : ""
    const type = typeof spec.type === "string" ? spec.type : ""
    if (!name || !type) return []
    const providedBy = spec.provided_by
    const description =
      typeof spec.description === "string" ? spec.description : null
    const producer =
      typeof spec.producer_agent_name === "string"
        ? spec.producer_agent_name
        : null
    return [
      {
        slot:
          typeof spec.service_uri === "string" && spec.service_uri
            ? spec.service_uri
            : name,
        type,
        description: description || null,
        provided_by:
          providedBy === "publisher" || providedBy === "template"
            ? providedBy
            : "user",
        producer_agent_name: producer || null,
      },
    ]
  })
}

/** "Odoo, IMAP": the distinct credential types an item needs, or `null`. */
export function catalogNeedsLine(
  specs: CatalogItem["required_credential_specs"],
): string | null {
  const labels = Array.from(
    new Set(
      catalogCredentialRequirements(specs).map((r) =>
        credentialTypeLabel(r.type),
      ),
    ),
  )
  return labels.length > 0 ? labels.join(", ") : null
}

/** The caller's cloud install of a cloud agent, if any. */
export function myCloudInstall(item: CatalogItem) {
  return (item.my_installs ?? []).find((i) => i.where === "cloud") ?? null
}

/** Distinct computers the caller has this item on. */
export function myComputerCount(item: CatalogItem): number {
  return new Set(
    (item.my_installs ?? [])
      .filter((i) => i.where === "local" && i.device_id)
      .map((i) => i.device_id),
  ).size
}

/** Distinct agents of the caller that use this skill. */
export function mySkillAgentCount(item: CatalogItem): number {
  return new Set(
    (item.my_installs ?? [])
      .filter((i) => i.where === "cloud" && i.install_id)
      .map((i) => i.install_id),
  ).size
}

/** A cloud install behind the latest version: the tile offers Update. */
export function hasUpdate(item: CatalogItem): boolean {
  const install = myCloudInstall(item)
  if (!install || install.revision_number == null || !item.latest) return false
  return install.revision_number < item.latest.revision_number
}

/** The one line a tile spends on the caller's own installs, or `null`. */
export function myInstallLine(item: CatalogItem): string | null {
  switch (catalogKindOf(item)) {
    case "cloud":
      if (!myCloudInstall(item)) return null
      return hasUpdate(item) ? "Installed · update available" : "Installed"
    case "desktop": {
      const n = myComputerCount(item)
      if (n === 0) return null
      return `On ${n} of your ${n === 1 ? "computer" : "computers"}`
    }
    case "skill": {
      const n = mySkillAgentCount(item)
      return n > 0 ? `Used in ${n} of my agents` : null
    }
  }
}

export type CatalogKindParam = "all" | CatalogKind
export type CatalogFilter = "all" | "public" | "shared" | "installed" | "mine"

export function parseCatalogKind(value: unknown): CatalogKindParam {
  return value === "cloud" || value === "desktop" || value === "skill"
    ? value
    : "all"
}

export function parseCatalogFilter(value: unknown): CatalogFilter {
  return value === "public" ||
    value === "shared" ||
    value === "installed" ||
    value === "mine"
    ? value
    : "all"
}

export function matchesCatalogKind(
  item: CatalogItem,
  kind: CatalogKindParam,
): boolean {
  return kind === "all" || catalogKindOf(item) === kind
}

export function matchesCatalogFilter(
  item: CatalogItem,
  filter: CatalogFilter,
): boolean {
  switch (filter) {
    case "public":
      return item.visibility === "public"
    case "shared":
      return item.visibility === "users"
    case "installed":
      return (item.my_installs ?? []).length > 0
    case "mine":
      return item.access === "edit" || item.access === "maintain"
    default:
      return true
  }
}

/** Where an item's detail lives: one entity, one page. */
export function catalogDetailTarget(
  item: Pick<CatalogItem, "kind" | "runs_on" | "uuid" | "id">,
):
  | { to: "/catalog/items/$uuid"; params: { uuid: string } }
  | { to: "/catalog/skills/$packageId"; params: { packageId: string } }
  | { to: "/catalog/agents/install/$bundleId"; params: { bundleId: string } } {
  switch (catalogKindOf(item)) {
    case "desktop":
      return { to: "/catalog/items/$uuid", params: { uuid: item.uuid } }
    case "skill":
      return {
        to: "/catalog/skills/$packageId",
        params: { packageId: item.uuid },
      }
    default:
      return {
        to: "/catalog/agents/install/$bundleId",
        params: { bundleId: item.id },
      }
  }
}

/**
 * `["catalog-items", filters]`. Every list shares the prefix, so a socket
 * `catalog.updated` or a write invalidates them all with `["catalog-items"]`.
 */
export function catalogItemsQueryKey(scope?: "all") {
  return ["catalog-items", scope ? { scope } : {}] as const
}

export function catalogItemQueryKey(uuid: string) {
  return ["catalog-item", uuid] as const
}

/** The desktop app's deep link for one catalog item. */
export function cinnaCatalogLink(item: Pick<CatalogItem, "id">): string {
  return `cinna://catalog/${encodeURIComponent(item.id)}`
}

/**
 * What the maintainer surfaces (People, delete, list/delist) need to know
 * about an item. A skill package's detail and a cloud install context can
 * build one without fetching the full `CatalogItem`.
 */
export type CatalogItemRef = Pick<
  CatalogItem,
  "uuid" | "kind" | "runs_on" | "display_name" | "visibility" | "is_listed"
>

/** "agent" or "skill", for a sentence about the item. */
export function catalogNoun(item: Pick<CatalogItem, "kind">): string {
  return item.kind === "skill" ? "skill" : "agent"
}
