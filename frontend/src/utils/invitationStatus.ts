/**
 * The invitation status vocabulary, in one place.
 *
 * The backend derives an invitation's status in exactly one function
 * (`InvitationService.status_of`) from one tuple of constants
 * (`INVITATION_STATUS_*` in `app/models/users/user_invitation.py`). It reaches
 * the frontend as a bare `string | null` — on `UserPublic.invitation_status`
 * and on `UserInvitationPublic.status` — so TypeScript cannot tell us when a
 * fifth value appears.
 *
 * That is the whole reason this file exists. A `switch` written inline over
 * string literals will happily fall through to a `default` badge, and a value
 * the server added last week renders as a plausible-looking label that is
 * simply wrong. Every surface reads the vocabulary from here instead, and
 * `knownInvitationStatus` returns `null` for anything it does not recognise —
 * which callers must render as *nothing*, never as a default state.
 */

export const INVITATION_STATUS_PENDING = "pending"
export const INVITATION_STATUS_ACCEPTED = "accepted"
export const INVITATION_STATUS_REVOKED = "revoked"
export const INVITATION_STATUS_EXPIRED = "expired"

/** The four values this build knows how to render. */
export const INVITATION_STATUSES = [
  INVITATION_STATUS_PENDING,
  INVITATION_STATUS_ACCEPTED,
  INVITATION_STATUS_REVOKED,
  INVITATION_STATUS_EXPIRED,
] as const

export type InvitationStatus = (typeof INVITATION_STATUSES)[number]

/**
 * Narrow an off-the-wire status to one this build understands.
 *
 * Returns `null` for `null`, `undefined` and — deliberately — for any value
 * the server has learned since this build shipped. Callers must treat `null`
 * as "say nothing", never as a default state.
 */
export function knownInvitationStatus(
  status: string | null | undefined,
): InvitationStatus | null {
  if (!status) return null
  return (INVITATION_STATUSES as readonly string[]).includes(status)
    ? (status as InvitationStatus)
    : null
}

/**
 * Whether an admin still has something to act on.
 *
 * Every non-accepted state, `revoked` included. That is not a UI choice —
 * `InvitationService.resend` rotates the jti, extends the expiry *and clears
 * `revoked_at`*, which is what makes it "the repair action for every
 * non-accepted state"; only acceptance is refused. Leaving `revoked` out
 * strands the row: the menu offers nothing, and re-inviting the address hits
 * the duplicate-email refusal whose own hint points back at this menu.
 *
 * A value this build does not recognise gets no actions rather than a guess.
 */
export function isOutstandingInvitation(
  status: string | null | undefined,
): boolean {
  const known = knownInvitationStatus(status)
  return (
    known === INVITATION_STATUS_PENDING ||
    known === INVITATION_STATUS_EXPIRED ||
    known === INVITATION_STATUS_REVOKED
  )
}

/**
 * True when the server reported a status this build has never heard of.
 *
 * Distinct from "no invitation at all": the caller has to be able to stay
 * silent about an unknown value instead of falling back to a label that
 * asserts something about the account.
 */
export function isUnknownInvitationStatus(
  status: string | null | undefined,
): boolean {
  return Boolean(status) && knownInvitationStatus(status) === null
}

/**
 * Whole days from now until `iso`, negative once it is in the past.
 *
 * Rounded up, so an invitation with six hours left reads "1 day" rather than
 * "0 days" — a countdown that hits zero while the link still works is worse
 * than one that is a few hours generous.
 */
export function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null
  const target = new Date(iso).getTime()
  if (Number.isNaN(target)) return null
  return Math.ceil((target - Date.now()) / 86_400_000)
}

/** "in 3 days" / "in 1 day" / "today" / "2 days ago", or null when unknown. */
export function formatDaysUntil(iso: string | null | undefined): string | null {
  const days = daysUntil(iso)
  if (days === null) return null
  if (days === 0) return "today"
  if (days > 0) return `in ${days} day${days === 1 ? "" : "s"}`
  const past = Math.abs(days)
  return `${past} day${past === 1 ? "" : "s"} ago`
}
