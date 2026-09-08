import type { AIProviderPublic, MembershipProvisioningStatus } from "@/client"
import { membershipStatusMeta } from "@/utils/keyProvisioning"

/**
 * "Is anything still being created for this provider?"
 *
 * All that survives of this module. It used to also hold the record list's
 * key-state summary and its per-member chip — one chip per member inside a
 * single table cell, unbounded, each carrying its own inline Retry. Both went
 * with `LlmProvidersTable`: a member is not a row on a record, and a key now
 * has a row of its own on the Keys tab (`Admin/AiKeys/`).
 *
 * A provider projection carries counts per membership status rather than the
 * member rows, which is why this reads a summary — but it reads `inFlight` off
 * `membershipStatusMeta` rather than listing which statuses are still moving.
 * That list is the thing that drifts.
 *
 * A status the server sends that this build has never heard of resolves to the
 * fallback meta, which is deliberately not-in-flight: guessing "in progress"
 * for a status nobody has described would poll forever.
 */
export function hasProviderKeyInFlight(provider: AIProviderPublic): boolean {
  return Object.entries(provider.key_state_summary ?? {}).some(
    ([status, count]) =>
      count > 0 &&
      membershipStatusMeta(status as MembershipProvisioningStatus).inFlight,
  )
}
