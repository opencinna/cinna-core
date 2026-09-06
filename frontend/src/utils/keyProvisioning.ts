import type { MembershipProvisioningStatus } from "@/client"

/**
 * Per-user key provisioning, as both audiences read it.
 *
 * Shared by the admin surfaces and by the settings page the key is *for*, so
 * the two never disagree about what a status means. It lives here rather than
 * under `components/Admin` for that reason: one table, one place, two readers.
 */

/** React Query key for the current user's in-flight / failed key provisionings. */
export const MY_KEY_PROVISIONINGS_QUERY_KEY = ["myKeyProvisionings"] as const

/**
 * How each membership status reads, to an administrator and to the person the
 * key is for.
 *
 * A `Record` over the generated union rather than a lookup with a fallback:
 * a seventh status added on the server stops compiling here instead of
 * rendering as a raw identifier in two places. The two labels live in one
 * entry for the same reason — an admin's "Key failed" and an owner's "Setup
 * failed" are one fact worded for two audiences, and splitting them into two
 * tables is how the two start disagreeing about which status they describe.
 *
 * `tone` drives the badge, and is deliberately not a colour: `pending` and
 * `minting` are both "in progress", and callers must not have to know which
 * of the six is which.
 */
export const MEMBERSHIP_STATUS_META: Record<
  MembershipProvisioningStatus,
  {
    adminLabel: string
    ownerLabel: string
    tone: "neutral" | "progress" | "ok" | "error"
    /** Still moving. `failed` is terminal and is not in flight. */
    inFlight: boolean
  }
> = {
  not_applicable: {
    adminLabel: "Shared key",
    ownerLabel: "Ready",
    tone: "neutral",
    inFlight: false,
  },
  pending: {
    adminLabel: "Key queued",
    ownerLabel: "Being set up",
    tone: "progress",
    inFlight: true,
  },
  minting: {
    adminLabel: "Creating key",
    ownerLabel: "Being set up",
    tone: "progress",
    inFlight: true,
  },
  provisioned: {
    adminLabel: "Key created",
    ownerLabel: "Ready",
    tone: "ok",
    inFlight: false,
  },
  failed: {
    adminLabel: "Key failed",
    ownerLabel: "Setup did not finish",
    tone: "error",
    inFlight: false,
  },
  suspended: {
    adminLabel: "Suspended",
    ownerLabel: "Suspended",
    tone: "neutral",
    inFlight: false,
  },
}

/**
 * Coarse reason codes the backend records in `last_error`, in the vocabulary of
 * the person reading them. Unknown codes are shown verbatim — a code nobody has
 * written copy for is still more useful than "an error occurred".
 */
const PROVISION_ERROR_COPY: Record<string, string> = {
  no_admin_credential:
    "No provider admin key is connected for this provider any more.",
  project_not_capped:
    "The provider project has no enforcing monthly spend limit, so no key was created.",
  mint_failed: "The provider refused to create the key.",
  provider_error: "The provider returned an unexpected response.",
  invalid_admin_secret: "The provider rejected the admin key.",
}

export function describeProvisionError(code: string | null | undefined): string {
  if (!code) return "No reason was recorded."
  return PROVISION_ERROR_COPY[code] ?? code
}

/**
 * The meta for a status, with a survivable answer for one nobody has copy for.
 *
 * The `Record` above is the drift-catcher and stays that way: a seventh status
 * added on the server fails the build here. This is for the other case — a
 * *runtime* value outside the union, which the compiler cannot rule out because
 * it comes off the wire. A bare `MEMBERSHIP_STATUS_META[row.status]` returns
 * `undefined` for one, and the `.inFlight` / `.tone` access on the next line is
 * what throws — inside a `refetchInterval` callback, so an unknown status did
 * not degrade a badge: it took down the settings page for the person whose key
 * was in that state.
 *
 * The fallback deliberately reads as neutral and not-in-flight. Guessing
 * "in progress" for a status nobody has described would spin forever.
 */
export function membershipStatusMeta(
  status: MembershipProvisioningStatus,
): (typeof MEMBERSHIP_STATUS_META)[MembershipProvisioningStatus] {
  return (
    MEMBERSHIP_STATUS_META[status] ?? {
      adminLabel: status,
      ownerLabel: status,
      tone: "neutral",
      inFlight: false,
    }
  )
}
