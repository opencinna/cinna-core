import type { LLMPluginMarketplaceCreate } from "@/client"

/**
 * Shared copy for addon marketplaces and the entries they publish.
 *
 * A marketplace now comes in three formats, and the same two facts have to be
 * said in four places that do not otherwise share a file: the create dialog's
 * Format field, the admin table's Format column, the entries table's Supported
 * column, and the Add addon dialog's refused-result row. A second copy of the
 * reason sentences is how a user is told "published to npm" in one place and
 * "unsupported" in another for the same row.
 *
 * Nothing here matches on prose. The server stores a stable code
 * (`unsupported_reason`) and answers a refused install with its own sentence;
 * these are the words the client prints when it holds only the code, and they
 * are kept in step with `UNSUPPORTED_REASON_SENTENCES` in
 * `backend/app/services/plugins/llm_plugin_service.py`.
 */

/**
 * The `type` a marketplace is created with, taken from the generated client.
 *
 * Derived rather than re-declared so a format added or removed on the server
 * cannot silently diverge from the words this module prints for it.
 * `NonNullable` because the field is optional on the wire (it has a
 * server-side default). The generated `export type type` is deliberately not
 * used: it is a generator artefact whose name will collide the moment the
 * spec grows another inline enum.
 */
export type MarketplaceFormat = NonNullable<LLMPluginMarketplaceCreate["type"]>

export interface MarketplaceFormatOption {
  value: MarketplaceFormat
  /** The words in the Format field and the Format column. */
  label: string
  /** What the repository must contain for this format to parse. */
  help: string
}

/**
 * Every format's words, keyed by the generated union.
 *
 * This `Record` is the drift-catcher in the *additive* direction: a format
 * added on the server is a missing key here and fails to compile, rather than
 * quietly vanishing from the create dialog and printing as a raw code in the
 * Format column.
 */
const FORMAT_COPY: Record<MarketplaceFormat, { label: string; help: string }> =
  {
    claude: {
      label: "Claude plugins",
      help: "Expects .claude-plugin/marketplace.json in the repository root.",
    },
    codex: {
      label: "Codex plugins",
      help: "Expects .agents/plugins/marketplace.json in the repository root.",
    },
    skills: {
      label: "Skills repository",
      help: "Expects a directory of SKILL.md folders.",
    },
  }

/**
 * The formats in the order the field offers them.
 *
 * `claude` is first and is the default: it is what every marketplace on every
 * instance is today, and a create dialog whose default is the rare case is a
 * dialog that has to be corrected on every use.
 *
 * A tuple rather than a bare union so the `zod` schema in `AddMarketplace` can
 * reuse it instead of writing the three strings a third time, and
 * `satisfies` is the drift-catcher in the *subtractive* direction: a value
 * removed from the server's enum stops satisfying this and fails to compile.
 */
export const MARKETPLACE_FORMAT_VALUES = [
  "claude",
  "codex",
  "skills",
] as const satisfies readonly MarketplaceFormat[]

export const MARKETPLACE_FORMATS: MarketplaceFormatOption[] =
  MARKETPLACE_FORMAT_VALUES.map((value) => ({ value, ...FORMAT_COPY[value] }))

/** What the chosen format expects to find in the repository. */
export function marketplaceFormatHelp(value: MarketplaceFormat): string {
  return FORMAT_COPY[value].help
}

/**
 * A marketplace's format in words, or the raw value when this build has not
 * heard of it.
 *
 * The raw value rather than "Unknown": a row created by a newer server is
 * still a real row, and an admin reading its own stored `type` can act on it,
 * where "Unknown" would send them looking for a bug.
 */
export function marketplaceFormatLabel(
  type: string | null | undefined,
): string {
  // Takes `string`, not `MarketplaceFormat`: the *read* path is a bare
  // `string` on the wire (`LLMPluginMarketplacePublic.type`) even though
  // create and update are the enum, so this is the one side that cannot be
  // checked and has to degrade instead.
  //
  // Absent rather than unrecognised: `type` is required on the wire, so this
  // only fires for a row stored before the column had a value, whose format
  // was the Claude parser by definition.
  if (!type) return FORMAT_COPY.claude.label
  return type in FORMAT_COPY
    ? FORMAT_COPY[type as MarketplaceFormat].label
    : type
}

/**
 * Why an entry cannot be installed, keyed by the server's stable code.
 *
 * `unsafe_path` is an entry whose path is absolute or escapes the repo,
 * refused per entry so its siblings still sync. `too_large` is a skills-format
 * entry whose folder is over the 16 MB snapshot cap.
 */
const UNSUPPORTED_REASON_COPY: Record<string, string> = {
  npm_source:
    "This entry is published to npm, which agent environments cannot install from.",
  app_connector_only:
    "This entry only declares app connectors, which this platform does not run.",
  no_skill_md:
    "This entry has no valid SKILL.md, so there is nothing to install.",
  unknown_source:
    "This entry declares a source this platform cannot fetch from.",
  unsafe_path:
    "This entry points outside its repository, so it cannot be fetched safely.",
  too_large: "This skill is larger than 16 MB, so it cannot be installed.",
}

const UNSUPPORTED_FALLBACK =
  "This marketplace entry cannot be installed by this platform."

/**
 * The sentence for an unsupported entry.
 *
 * Degrades to the generic sentence for a code this build has not heard of —
 * the server may learn a new refusal before the client is redeployed, and
 * printing the bare code at a user is worse than printing a true generality.
 */
export function unsupportedReasonSentence(
  code: string | null | undefined,
): string {
  return UNSUPPORTED_REASON_COPY[code ?? ""] ?? UNSUPPORTED_FALLBACK
}

/**
 * An entry's content snapshot — the archive Cinna Desktop downloads — as a
 * label and a dot tone. Independent of `supported` above, which keeps its
 * cloud-install meaning: an entry can be "Not installable" in the cloud and
 * still have a ready snapshot.
 *
 * `null` for a row with no snapshot state (a format with no snapshot, or a row
 * not re-synced since the columns were added): absent facts render nothing.
 */

/**
 * Own-key lookup for the copy tables, so a code like `constructor` or
 * `toString` cannot match a prototype key. Not `Object.hasOwn`: that is
 * ES2022 and the build's lib is ES2020.
 */
function hasOwn(table: object, key: string): boolean {
  return Object.keys(table).includes(key)
}

export type SnapshotStatus = "pending" | "ready" | "failed"

const SNAPSHOT_STATUS_COPY: Record<
  SnapshotStatus,
  { label: string; tone: "on" | "off" | "error" }
> = {
  ready: { label: "Ready", tone: "on" },
  pending: { label: "Pending", tone: "off" },
  failed: { label: "Failed", tone: "error" },
}

export function snapshotStatusMeta(status: string | null | undefined): {
  status: SnapshotStatus
  label: string
  tone: "on" | "off" | "error"
} | null {
  if (!status || !hasOwn(SNAPSHOT_STATUS_COPY, status)) return null
  const known = status as SnapshotStatus
  return { status: known, ...SNAPSHOT_STATUS_COPY[known] }
}

/**
 * Why a snapshot failed, keyed by the server's stable `snapshot_reason`
 * (`backend/app/models/plugins/llm_plugin.py`). A `skills`-format row carries
 * its `unsupported_reason` code here, so an unknown code falls through to that
 * table before the generic sentence.
 */
const SNAPSHOT_REASON_COPY: Record<string, string> = {
  no_components:
    "This entry ships no skills, commands, agents, hooks or servers, so there is nothing to deliver.",
  unsafe_path:
    "This entry contains a path outside its folder or a symlink, so it cannot be packed safely.",
  too_large: "This entry is larger than 16 MB, so it cannot be packed.",
  fetch_failed:
    "Its source could not be fetched; it can be retried from the marketplace's Plugins tab.",
}

export function snapshotReasonSentence(
  code: string | null | undefined,
): string {
  if (code && hasOwn(SNAPSHOT_REASON_COPY, code))
    return SNAPSHOT_REASON_COPY[code]
  if (code && hasOwn(UNSUPPORTED_REASON_COPY, code)) {
    return UNSUPPORTED_REASON_COPY[code]
  }
  return "The snapshot of this entry could not be taken."
}

/** An MCP server's transport as printed: "stdio", "HTTP", "SSE". */
export function mcpTransportLabel(transport: string): string {
  if (transport === "http" || transport === "streamable-http") return "HTTP"
  if (transport === "sse") return "SSE"
  return transport
}

const MCP_AUTH_COPY: Record<string, string> = {
  none: "no auth",
  env: "env vars",
  oauth: "OAuth",
  bearer: "bearer token",
}

/** How an MCP server authenticates, in words; the raw code when unknown. */
export function mcpAuthLabel(auth: string | null | undefined): string | null {
  if (!auth) return null
  return MCP_AUTH_COPY[auth] || auth
}

/** "1 entry" / "3 entries". */
export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`
}

/**
 * An entry's access as the user experiences it — the wire word `default` is
 * never printed (UI spec S7 label table). Shared by the marketplace entries
 * list (S1) and the MCP directory (S4), so a banned plugin and a banned server
 * read alike. `mode` is the marketplace's access mode; an MCP directory entry
 * has none and reads like `allow_all`.
 */
export function entryAccessLabel(
  access: string | null | undefined,
  mode?: "allow_all" | "allowlist" | null,
): { label: string; tone: "on" | "warning" | "error" } {
  if (access === "banned") return { label: "Banned", tone: "error" }
  if (access === "allowed") return { label: "Allowed", tone: "on" }
  if (mode === "allowlist") {
    return { label: "Waiting for approval", tone: "warning" }
  }
  return { label: "Visible", tone: "on" }
}
