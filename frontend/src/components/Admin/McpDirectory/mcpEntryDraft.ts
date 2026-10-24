import type {
  McpDirectoryEntryCreate,
  McpDirectoryEntryPublic,
  McpDirectoryEntryUpdate,
  McpEnvVar,
  McpPackage,
} from "@/client"

/**
 * The form model shared by the Add wizard (S5) and the Edit Sheet (S6), so the
 * two cannot drift: one draft shape, one validator, one mapping to each wire
 * body. Validation mirrors `McpDirectoryService._validate` on the server
 * (`backend/app/services/mcp/mcp_directory_service.py`) so most refusals are
 * said beside the field instead of as a toast after the round trip.
 */

export type McpTransport = McpDirectoryEntryCreate["transport"]
export type McpAuth = NonNullable<McpDirectoryEntryCreate["auth"]>
export type McpRegistry = McpPackage["registry"]

export interface McpEnvRow {
  /** Client-only identity, so removing row 2 does not re-key row 3's inputs. */
  key: string
  name: string
  secret: boolean
  description: string
}

export interface McpEntryDraft {
  transport: McpTransport | null
  // http / sse
  url: string
  auth: McpAuth
  // stdio
  registry: McpRegistry
  packageName: string
  version: string
  /** One argument per line. */
  args: string
  env: McpEnvRow[]
  // listing
  title: string
  name: string
  description: string
  iconUrl: string
  /** Comma separated. */
  categories: string
  homepage: string
  publisher: string
  verified: boolean
}

export type McpDraftField =
  | "transport"
  | "url"
  | "packageName"
  | "title"
  | "name"
  | "iconUrl"
  | "homepage"
  | "categories"

export type McpDraftErrors = Partial<Record<McpDraftField, string>> & {
  /** Per env row, keyed by `McpEnvRow.key`. */
  env?: Record<string, string>
  /** The list as a whole is over the cap. */
  envCount?: string
}

/** Caps the server enforces (`McpDirectoryEntryCreate`). */
export const MAX_CATEGORIES = 20
export const MAX_ENV = 50

export const NAME_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/
export const ENV_NAME_PATTERN = /^[A-Z_][A-Z0-9_]*$/

let envRowSeq = 0
export function newEnvRow(from?: McpEnvVar): McpEnvRow {
  envRowSeq += 1
  return {
    key: `env-${envRowSeq}`,
    name: from?.name || "",
    secret: from?.secret === true,
    description: from?.description || "",
  }
}

export function emptyDraft(): McpEntryDraft {
  return {
    transport: null,
    url: "",
    auth: "none",
    registry: "npm",
    packageName: "",
    version: "",
    args: "",
    env: [],
    title: "",
    name: "",
    description: "",
    iconUrl: "",
    categories: "",
    homepage: "",
    publisher: "",
    verified: false,
  }
}

function asTransport(value: string): McpTransport | null {
  return value === "http" || value === "sse" || value === "stdio" ? value : null
}

function asAuth(value: string): McpAuth {
  return value === "oauth" || value === "bearer" || value === "env"
    ? value
    : "none"
}

export function draftFromEntry(entry: McpDirectoryEntryPublic): McpEntryDraft {
  const pkg = entry.package
  return {
    transport: asTransport(entry.transport),
    url: entry.url || "",
    auth: asAuth(entry.auth),
    registry: pkg?.registry || "npm",
    packageName: pkg?.name || "",
    version: pkg?.version || "",
    args: (pkg?.args ?? []).join("\n"),
    env: (entry.env ?? []).map((item) => newEnvRow(item)),
    title: entry.title,
    name: entry.name,
    description: entry.description || "",
    iconUrl: entry.icon_url || "",
    categories: (entry.categories ?? []).join(", "),
    homepage: entry.homepage || "",
    publisher: entry.publisher_name || "",
    verified: entry.verified,
  }
}

/** "Linear MCP" → "linear-mcp", capped at the server's 64 characters. */
export function slugify(title: string): string {
  return title
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .replace(/-+$/, "")
    .slice(0, 64)
}

function isHttps(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === "https:" && url.host !== ""
  } catch {
    return false
  }
}

function splitCategories(value: string): string[] {
  return value
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean)
}

/** The connection half: the transport's own fields and the env list. */
export function validateConnection(draft: McpEntryDraft): McpDraftErrors {
  const errors: McpDraftErrors = {}
  if (!draft.transport) errors.transport = "Choose how the server connects."
  if (draft.transport === "http" || draft.transport === "sse") {
    if (!draft.url.trim()) errors.url = "Enter the server's URL."
    else if (!isHttps(draft.url.trim())) errors.url = "Use an https:// URL"
  }
  if (draft.transport === "stdio" && !draft.packageName.trim()) {
    errors.packageName = "Enter the package name."
  }
  const env: Record<string, string> = {}
  const seen = new Set<string>()
  for (const row of draft.env) {
    const name = row.name.trim()
    if (!ENV_NAME_PATTERN.test(name)) {
      env[row.key] =
        "Upper case letters, digits and _, not starting with a digit"
    } else if (seen.has(name)) {
      env[row.key] = "Listed twice"
    }
    seen.add(name)
  }
  if (Object.keys(env).length > 0) errors.env = env
  if (draft.env.length > MAX_ENV) {
    errors.envCount = `At most ${MAX_ENV} variables`
  }
  return errors
}

/** The listing half: what users read in the directory. */
export function validateListing(
  draft: McpEntryDraft,
  { checkName }: { checkName: boolean },
): McpDraftErrors {
  const errors: McpDraftErrors = {}
  if (!draft.title.trim()) errors.title = "Enter a title."
  if (checkName) {
    if (!draft.name) errors.name = "Enter a name."
    else if (!NAME_PATTERN.test(draft.name)) {
      errors.name =
        "Lower case letters, digits, '.', '_' and '-', starting with a letter or digit"
    }
  }
  if (draft.iconUrl.trim() && !isHttps(draft.iconUrl.trim())) {
    errors.iconUrl = "Use an https:// URL"
  }
  if (draft.homepage.trim() && !isHttps(draft.homepage.trim())) {
    errors.homepage = "Use an https:// URL"
  }
  const categories = splitCategories(draft.categories)
  if (categories.some((c) => c.length > 64)) {
    errors.categories = "Each category is at most 64 characters"
  } else if (categories.length > MAX_CATEGORIES) {
    errors.categories = `At most ${MAX_CATEGORIES} categories`
  }
  return errors
}

export function hasErrors(errors: McpDraftErrors): boolean {
  return Object.keys(errors).length > 0
}

/** Optional text: an empty field is "absent", sent as `null`. */
function optional(value: string): string | null {
  return value.trim() || null
}

function envOf(draft: McpEntryDraft): McpEnvVar[] {
  return draft.env.map((row) => ({
    name: row.name.trim(),
    secret: row.secret,
    description: optional(row.description),
  }))
}

/**
 * Auth for stdio is not asked: a local process authenticates with what it is
 * given in its environment, so it is `env` when any variable is secret.
 */
function authOf(draft: McpEntryDraft): McpAuth {
  if (draft.transport === "stdio") {
    return draft.env.some((row) => row.secret) ? "env" : "none"
  }
  return draft.auth
}

function connectionOf(draft: McpEntryDraft) {
  const transport = draft.transport as McpTransport
  const stdio = transport === "stdio"
  return {
    transport,
    url: stdio ? null : draft.url.trim(),
    package: stdio
      ? {
          registry: draft.registry,
          name: draft.packageName.trim(),
          version: optional(draft.version),
          args: draft.args
            .split("\n")
            .map((a) => a.trim())
            .filter(Boolean),
        }
      : null,
    auth: authOf(draft),
    env: envOf(draft),
  }
}

function listingOf(draft: McpEntryDraft) {
  return {
    title: draft.title.trim(),
    description: optional(draft.description),
    icon_url: optional(draft.iconUrl),
    categories: splitCategories(draft.categories),
    homepage: optional(draft.homepage),
    publisher_name: optional(draft.publisher),
    verified: draft.verified,
  }
}

export function toCreateBody(draft: McpEntryDraft): McpDirectoryEntryCreate {
  return { name: draft.name, ...listingOf(draft), ...connectionOf(draft) }
}

/** The whole configuration in update form; `name` is immutable. */
function fullUpdate(draft: McpEntryDraft): McpDirectoryEntryUpdate {
  return { ...listingOf(draft), ...connectionOf(draft) }
}

/**
 * Only the fields that differ from `initial`: the server reads an omitted
 * field as unchanged and refuses an explicit `null` on a required one, so
 * an untouched field is never sent. A transport switch carries its url /
 * package along, since both differ from the old transport's.
 */
export function toUpdateBody(
  draft: McpEntryDraft,
  initial: McpEntryDraft,
): McpDirectoryEntryUpdate {
  const next = fullUpdate(draft)
  const before = fullUpdate(initial)
  const changed: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof McpDirectoryEntryUpdate)[]) {
    if (JSON.stringify(next[key]) !== JSON.stringify(before[key])) {
      changed[key] = next[key]
    }
  }
  return changed as McpDirectoryEntryUpdate
}

/** Dirty tracking compares what would be sent, not the raw keystrokes. */
export function draftSignature(draft: McpEntryDraft): string {
  return JSON.stringify({ name: draft.name, ...fullUpdate(draft) })
}
