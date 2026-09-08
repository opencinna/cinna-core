import type { AdminAIKeyRow, MembershipProvisioningStatus } from "@/client"
import { membershipStatusMeta } from "@/utils/keyProvisioning"

/**
 * Query keys and row helpers for the keys list.
 *
 * The prefix is deliberately separate from `MANAGED_CREDENTIALS_QUERY_PREFIX`:
 * the two surfaces read two different projections of the same rows, and every
 * per-key verb invalidates *both* — a revoke changes the key list and the
 * record's member count, and a cache that only knew about one of them would
 * leave the other stating something that stopped being true.
 */
export const AI_KEYS_QUERY_PREFIX = ["admin", "ai-keys"] as const

export interface AiKeysFilters {
  q: string
  status: MembershipProvisioningStatus | "all"
  kind: "all" | "per_user" | "shared"
  providerId: string | "all"
}

export const EMPTY_AI_KEYS_FILTERS: AiKeysFilters = {
  q: "",
  status: "all",
  kind: "all",
  providerId: "all",
}

export function hasActiveFilters(filters: AiKeysFilters): boolean {
  return (
    filters.q.trim() !== "" ||
    filters.status !== "all" ||
    filters.kind !== "all" ||
    filters.providerId !== "all"
  )
}

export function aiKeysQueryKey(
  filters: AiKeysFilters,
  page: number,
  pageSize: number,
) {
  return [...AI_KEYS_QUERY_PREFIX, filters, page, pageSize] as const
}

/**
 * The row's identity. `membership_id` for a per-user key, the record for a
 * shared one — the same pair the server publishes rather than one polymorphic
 * id, so nothing here has to guess which table a value came from.
 */
export function keyRowId(row: AdminAIKeyRow): string {
  return row.membership_id ?? row.managed_credential_id
}

/** Does any key on this page still have work in flight? */
export function pageHasKeyInFlight(rows: AdminAIKeyRow[]): boolean {
  return rows.some(
    (row) =>
      row.provisioning_status != null &&
      membershipStatusMeta(row.provisioning_status).inFlight,
  )
}
