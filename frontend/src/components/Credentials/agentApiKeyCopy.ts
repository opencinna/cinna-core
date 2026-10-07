/**
 * User-visible copy for the agent-api **external key** product, in one place.
 *
 * The product name is still a working name (plan D34: "Agent API Key" reads
 * better than "Agent API Credential" next to the other picker entries, and
 * "key" signals copy-me-somewhere). Every surface that names it — the global
 * New-credential picker, the producer's API access card, the credential detail
 * card, toasts and confirm dialogs — reads it from here, so renaming the
 * product is a one-line change.
 *
 * A *key* is the outward-facing sibling of a *connection*: a connection wires
 * one platform agent to another and is machine-only, while a key is copied by a
 * human into a laptop script, a server, or a cron job. A key is **issued to**
 * one person: it lands in their credentials, and only they can see its value.
 *
 * The key-status vocabulary (plan D4, §8) lives here too, so the issuer's API
 * access card, the holder's detail page and both credential lists say the same
 * words for the same state.
 */

import type { AgentApiKeyStatus } from "@/client"

/** Product name, singular. */
export const AGENT_API_KEY_LABEL = "Agent API Key"

/** Product name, plural — section headers and list captions. */
export const AGENT_API_KEY_LABEL_PLURAL = "Agent API Keys"

/**
 * One-line "what is this for". The New-credential picker no longer shows it —
 * the key sits inside the "API & Access" group as an ordinary pill, since from
 * the outside it is just an API key for external use — so there it only seeds
 * the entry's search keywords ("code", "REST" find it). Kept as the canonical
 * phrasing for any surface that does want a one-liner.
 */
export const AGENT_API_KEY_TAGLINE =
  "call an agent's REST API from your own code"

/**
 * The curl a holder runs. ``baseUrl`` must be the PUBLIC base URL the backend
 * returned (``AgentApiTokenService.build_base_url``) — never a
 * container-rewritten one, which is unreachable from outside Docker.
 *
 * A ``null`` ``token`` yields a ``<your-key>`` placeholder. That output is for
 * DISPLAY ONLY — it is what the masked `<pre>` shows. It must never reach the
 * clipboard: copying is expected to produce a runnable command, and a snippet
 * that looks right but 401s is worse than no snippet.
 *
 * So the Copy button passes ``null`` as its `value` while the key is hidden and
 * resolves the real token first (see ``AgentApiKeyUsage``). Do not "simplify"
 * that call site into passing this function's result unconditionally — because
 * it always returns a string, that would short-circuit ``value ?? resolve()``
 * and ship the placeholder.
 */
export function buildAgentApiKeyCurl(
  baseUrl: string,
  token: string | null,
): string {
  const base = (
    baseUrl || "https://your-cinna-host/api/v1/agent-api/<agent-id>"
  ).replace(/\/+$/, "")
  return [
    `curl -H "Authorization: Bearer ${token ?? "<your-key>"}" \\`,
    `  ${base}/<your-endpoint>`,
  ].join("\n")
}

// ── Status vocabulary ───────────────────────────────────────────────────────

const STATUS_LABEL: Record<AgentApiKeyStatus, string> = {
  active: "Active",
  revoked_by_issuer: "Revoked by issuer",
  producer_removed: "Producer removed",
  issuer_removed: "Issuer account removed",
  expired: "Expired",
  api_disabled: "API turned off by producer",
  external_access_paused: "External access paused",
}

const PAUSED_STATUSES: ReadonlySet<string> = new Set<AgentApiKeyStatus>([
  "api_disabled",
  "external_access_paused",
])

/**
 * The status as words. Typed ``string`` on purpose: ``CredentialPublic``
 * carries it untyped, and a value this client has never heard of must still
 * read as something rather than as an empty badge.
 */
export function agentApiKeyStatusLabel(status: string): string {
  return STATUS_LABEL[status as AgentApiKeyStatus] || "Unavailable"
}

/**
 * The key no longer works and never will again — the four terminal values,
 * **and** any value this client does not know (plan §7: "unknown future
 * values: treat as unavailable").
 */
export function isTerminalAgentApiKeyStatus(status: string): boolean {
  return status !== "active" && !PAUSED_STATUSES.has(status)
}

/** The key is refused for now and recovers without anyone touching it. */
export function isPausedAgentApiKeyStatus(status: string): boolean {
  return PAUSED_STATUSES.has(status)
}

/**
 * One sentence saying what the status means for the holder and what to do.
 * ``expiredRelative`` is the already-formatted "3 days ago" — the caller owns
 * timestamp parsing (``formatRelativeTimestamp``, never ``new Date``).
 */
export function agentApiKeyStatusSentence(
  status: string,
  {
    issuerLabel,
    expiredRelative,
  }: { issuerLabel?: string | null; expiredRelative?: string | null } = {},
): string {
  switch (status) {
    case "active":
      return "This key works."
    case "revoked_by_issuer":
      return `${issuerLabel || "The issuer"} revoked this key. It no longer works — delete it, or ask them for a new one.`
    case "producer_removed":
      return "The agent this key called was deleted. The key no longer works — delete it."
    case "issuer_removed":
      return "The account that issued this key was removed. The key no longer works — delete it."
    case "expired":
      return `This key expired${expiredRelative ? ` ${expiredRelative}` : ""}. Delete it, or ask the issuer for a new one.`
    case "api_disabled":
      return "The producer turned its API off. The key works again when it is turned back on — nothing to do."
    case "external_access_paused":
      return "The producer paused external access. The key works again when it is turned back on — nothing to do."
    default:
      return "This key is unavailable. Delete it, or ask the issuer for a new one."
  }
}

// ── Slot ────────────────────────────────────────────────────────────────────

/**
 * DISPLAY-ONLY copy of the backend's default slot rule (plan D23), used as the
 * Give-access dialog's Slot placeholder. The dialog sends ``null`` when the
 * field is left empty, so the server stays the one source of the slug and a
 * drift here is cosmetic.
 *
 * Source of truth: ``AgentApiKeyService.default_service_uri`` in
 * ``backend/app/services/agent_api/agent_api_key_service.py`` — lowercase,
 * runs of non-``[a-z0-9]`` → ``-``, trim ``-``, cut at 40, trim again, empty →
 * ``agent``, then ``-api``. "CRM Agent" → "crm-agent-api".
 */
export function defaultAgentApiKeySlot(
  producerName: string | null | undefined,
): string {
  const trim = (s: string) => s.replace(/^-+|-+$/g, "")
  const slug = trim(
    trim((producerName || "").toLowerCase().replace(/[^a-z0-9]+/g, "-")).slice(
      0,
      40,
    ),
  )
  return `${slug || "agent"}-api`
}
