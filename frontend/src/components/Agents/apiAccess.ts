import type { AgentApiAccessEntry, AgentApiKeyPublic } from "@/client"
import type { RowStatus } from "@/components/Common/ListRow"
import { parseTimestamp } from "@/components/Common/RelativeTime"
import {
  agentApiKeyStatusLabel,
  isPausedAgentApiKeyStatus,
  isTerminalAgentApiKeyStatus,
} from "@/components/Credentials/agentApiKeyCopy"

/**
 * Shared reading of the producer's API access list (S1 card, its Sheet and the
 * person dialog), so the three cannot disagree about who is "active" or how a
 * person is named.
 */

/** Full name, else email, else a neutral word — wire strings use `||`. */
export function apiAccessPersonLabel(entry: AgentApiAccessEntry): string {
  return entry.user.full_name || entry.user.email || "Unknown user"
}

export function isEndedKey(key: AgentApiKeyPublic): boolean {
  return isTerminalAgentApiKeyStatus(key.status)
}

/** A key's dot as its issuer sees it: ended keys are history, not alarms. */
export function apiAccessKeyStatus(key: AgentApiKeyPublic): RowStatus {
  const label = agentApiKeyStatusLabel(key.status)
  if (key.status === "active") return { tone: "on", label }
  if (isPausedAgentApiKeyStatus(key.status)) return { tone: "warning", label }
  return { tone: "off", label }
}

export interface ApiAccessPersonSummary {
  liveKeys: AgentApiKeyPublic[]
  endedKeys: AgentApiKeyPublic[]
  /** Best key decides; `undefined` for a scopes-only person (no dot). */
  status: RowStatus | undefined
  /** Every key exists and every one has ended. */
  allEnded: boolean
  anyLiveReadOnly: boolean
  lastUsedAt: string | null
  linkedAgentCount: number
}

function latest(a: string | null, b: string | null): string | null {
  if (!a) return b
  if (!b) return a
  const da = parseTimestamp(a)?.getTime() ?? 0
  const db = parseTimestamp(b)?.getTime() ?? 0
  return db > da ? b : a
}

export function summarizeApiAccessPerson(
  entry: AgentApiAccessEntry,
): ApiAccessPersonSummary {
  const keys = entry.keys ?? []
  const liveKeys = keys.filter((k) => !isEndedKey(k))
  const endedKeys = keys.filter(isEndedKey)

  let status: RowStatus | undefined
  if (liveKeys.some((k) => k.status === "active")) {
    status = { tone: "on", label: "Has a working key" }
  } else if (liveKeys.length > 0) {
    status = { tone: "warning", label: "Keys paused" }
  } else if (endedKeys.length > 0) {
    status = { tone: "off", label: "All keys ended" }
  }

  return {
    liveKeys,
    endedKeys,
    status,
    allEnded: keys.length > 0 && liveKeys.length === 0,
    anyLiveReadOnly: liveKeys.some((k) => k.read_only),
    lastUsedAt: keys.reduce<string | null>(
      (acc, k) => latest(acc, k.last_used_at),
      null,
    ),
    linkedAgentCount: keys.reduce(
      (sum, k) => sum + (k.linked_agent_count ?? 0),
      0,
    ),
  }
}

const TONE_RANK: Record<string, number> = { on: 0, warning: 1 }

/**
 * Active first, then paused, then the most recently used, then by name — the
 * person a producer owner is most likely to be looking for leads.
 */
export function sortApiAccessEntries(
  entries: AgentApiAccessEntry[],
): AgentApiAccessEntry[] {
  const decorated = entries.map((entry) => {
    const s = summarizeApiAccessPerson(entry)
    return {
      entry,
      rank: TONE_RANK[s.status?.tone ?? ""] ?? 2,
      used: s.lastUsedAt ? (parseTimestamp(s.lastUsedAt)?.getTime() ?? 0) : 0,
      name: apiAccessPersonLabel(entry).toLowerCase(),
    }
  })
  decorated.sort(
    (a, b) =>
      a.rank - b.rank || b.used - a.used || a.name.localeCompare(b.name),
  )
  return decorated.map((d) => d.entry)
}

export { pluralize } from "@/utils/pluralize"
