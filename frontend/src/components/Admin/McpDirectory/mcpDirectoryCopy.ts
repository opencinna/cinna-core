import type { McpDirectoryEntryPublic } from "@/client"
import type { McpAuth, McpRegistry, McpTransport } from "./mcpEntryDraft"

/** Own-key lookup, so a code like `constructor` cannot match a prototype key. */
function lookup(table: Record<string, string>, key: string): string | null {
  return Object.keys(table).includes(key) ? table[key] : null
}

/** The query every MCP directory write invalidates. */
export const MCP_DIRECTORY_KEY = ["mcp-directory", "entries"] as const

/** Step 1 tiles (S5) and the Edit Sheet's transport `Select` (S6). */
export const TRANSPORT_COPY: Record<
  McpTransport,
  { label: string; help: string }
> = {
  http: {
    label: "Remote server (HTTP)",
    help: "A hosted server reached over streamable HTTP at an https:// URL.",
  },
  sse: {
    label: "Remote server (SSE)",
    help: "A hosted server that streams over server-sent events.",
  },
  stdio: {
    label: "Local process (stdio)",
    help: "A package from npm, PyPI or OCI that runs on the user's computer.",
  },
}

export const TRANSPORT_ORDER: readonly McpTransport[] = ["http", "sse", "stdio"]

/** Auth a remote server can ask for; stdio derives its own (`env` / `none`). */
export const REMOTE_AUTH_OPTIONS: readonly { value: McpAuth; label: string }[] =
  [
    { value: "none", label: "None" },
    { value: "oauth", label: "OAuth" },
    { value: "bearer", label: "Bearer token" },
  ]

const AUTH_LABEL: Record<string, string> = {
  none: "None",
  oauth: "OAuth",
  bearer: "Bearer token",
  env: "Environment",
}

/** The Auth column's words; the raw code when the server sends a new one. */
export function authLabel(auth: string): string {
  return lookup(AUTH_LABEL, auth) || auth
}

export const REGISTRY_OPTIONS: readonly {
  value: McpRegistry
  label: string
}[] = [
  { value: "npm", label: "npm" },
  { value: "pypi", label: "PyPI" },
  { value: "oci", label: "OCI" },
]

/** The Transport badge: "HTTP" / "SSE" / "stdio · npm". */
export function transportBadgeLabel(entry: McpDirectoryEntryPublic): string {
  if (entry.transport === "http") return "HTTP"
  if (entry.transport === "sse") return "SSE"
  if (entry.transport === "stdio") {
    const registry = REGISTRY_OPTIONS.find(
      (option) => option.value === entry.package?.registry,
    )
    return registry ? `stdio · ${registry.label}` : "stdio"
  }
  return entry.transport
}

/** "Manual" / "opencinna" — the Source column. */
export function sourceLabel(source: string): string {
  return source === "opencinna" ? "opencinna" : "Manual"
}

const SKIPPED_REASON: Record<string, string> = {
  no_remote: "Has no HTTPS endpoint",
  name_taken: "A server you added by hand uses this name",
  invalid_entry: "Its listing in the index is malformed",
  duplicate: "Listed twice in the index",
}

export function skippedReasonSentence(reason: string): string {
  return lookup(SKIPPED_REASON, reason) || "Could not be imported"
}
