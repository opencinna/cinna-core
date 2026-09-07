/**
 * The vocabulary Settings → Channels shares between its card, its rows and its
 * Configure Sheet: the scope wording, the labels for a person, the ordering of
 * a list of people, the narrowed patch type, and the one cache write every
 * channel mutation makes.
 *
 * THE ONE RULE THIS FEATURE IS ORGANISED AROUND
 * ---------------------------------------------
 * **The inherit rules are not implemented on the client, and must never be.**
 * Every value on `UserChannelPublic` arrives already resolved by the backend's
 * `ChannelPolicyService`, together with an `*_inherited` flag and the
 * `channel_default_*` value it followed. These files read those; they never
 * recompute "is this channel on for me" from a default and an override. A
 * second implementation of the rules is precisely how a settings page comes to
 * say a channel is on while the router treats it as off — a disagreement that
 * is undiagnosable from either side.
 */
import type { QueryClient } from "@tanstack/react-query"

import type {
  IdentityContactPublic,
  UserChannelPublic,
  UserChannelUpdate,
} from "@/client"

export const SCOPE_ALL = "all"
export const SCOPE_LIST = "list"
export const SCOPE_NONE = "none"

/** The `["userChannels"]` list, named once so every writer agrees on it. */
export const USER_CHANNELS_KEY = ["userChannels"] as const

/**
 * The single key for the people who have shared an identity with this user.
 *
 * The Configure Sheet reads it (for the "on but inert" warning), the People
 * card reads and writes it. Two keys for one list would let one surface show a
 * person the other has just switched off.
 */
export const IDENTITY_CONTACTS_KEY = ["identity-contacts"] as const

/**
 * User-facing wording for the three scopes.
 *
 * Deliberately not shared with the admin form's copy: the admin is choosing a
 * default for other people ("all their agents"), the user is choosing for
 * themselves ("all my agents"). Same values, different sentence.
 */
export const SCOPE_OPTIONS: ReadonlyArray<{
  value: string
  label: string
  hint: string
}> = [
  {
    value: SCOPE_ALL,
    label: "All my agents",
    hint: "Any agent I own can answer on this channel.",
  },
  {
    value: SCOPE_LIST,
    label: "Only the agents I pick",
    hint: "Just the agents ticked below.",
  },
  {
    value: SCOPE_NONE,
    label: "None",
    hint: "Nothing routes to me on this channel.",
  },
]

/**
 * The subset of `UserChannelUpdate` this surface is allowed to write.
 *
 * Derived from the generated type rather than hand-declared, so a change to
 * the wire model reaches these call sites — but narrowed with `Pick`, which is
 * what structurally keeps `pinned_agent_id` unwritable here (no UI sets it
 * yet), so a stray patch cannot pin an agent by accident.
 *
 * `allow_identity_routing` is what the Configure Sheet's Identity routing
 * section writes. `null` stays in the value type for the two inheritable
 * fields, where it is not "no value" but "revert this field to the channel
 * default" — it is NOT a legal value for `allow_identity_routing`, which has
 * no inherited state and is rejected by the API if sent as null, so that
 * section only ever sends a boolean.
 */
export type ChannelPatch = Pick<
  UserChannelUpdate,
  "is_enabled" | "agent_scope" | "agent_ids" | "allow_identity_routing"
>

/**
 * The user's switch says on, but the channel still isn't usable.
 *
 * The only remaining terms of the conjunction are the admin kill switch and
 * access, and neither is the user's doing — so this is reported, not folded
 * into the switch. Conflating them would tell someone their channel is off
 * when what actually happened is that it was taken away from them.
 */
export function isBlockedByAdmin(channel: UserChannelPublic): boolean {
  return channel.is_enabled && !channel.is_available
}

/** Shown wherever a blocked channel is: the row's badge, the Sheet's alert. */
export const UNAVAILABLE_SENTENCE =
  "You have this channel switched on, but it isn't available to you right now — an administrator has either disabled it or removed your access. Your setting is kept and takes effect again if that changes."

/**
 * The honest caption for an inheritable field, in one wording.
 *
 * An inherited value names the default it is following and says it will keep
 * following it; an explicit one says the default no longer applies. Both
 * fields that inherit (`is_enabled`, `agent_scope`) say it the same way, in
 * two different places — the row's tooltip and the Sheet's caption — so the
 * sentence lives here rather than being written twice and drifting.
 */
export function inheritCaption(
  inherited: boolean,
  defaultValue: string,
): string {
  return inherited
    ? `Following the administrator's default (${defaultValue}) — it changes if they change it.`
    : `You set this. The administrator's default is ${defaultValue}.`
}

export function scopeLabel(scope: string): string {
  // Falls through to the raw value rather than a blank: the scope vocabulary
  // is a plain string column that can grow without a client release.
  return SCOPE_OPTIONS.find((o) => o.value === scope)?.label ?? scope
}

/**
 * The resolved scope as the row's metadata line says it — the value, never its
 * provenance. Provenance is a badge on the row and a caption in the Sheet.
 */
export function scopeSummary(channel: UserChannelPublic): string {
  const picked = channel.agent_ids?.length ?? 0
  switch (channel.agent_scope) {
    case SCOPE_ALL:
      return "All my agents"
    case SCOPE_LIST:
      return picked === 1 ? "1 agent picked" : `${picked} agents picked`
    case SCOPE_NONE:
      return "No agents"
    default:
      return scopeLabel(channel.agent_scope)
  }
}

/** The rendered name for an identity contact — never a blank row. */
export function contactLabel(contact: IdentityContactPublic): string {
  // `owner_name` is non-nullable on the wire but may be blank, which is why
  // the fallback is on the trimmed value and not on `??`.
  return contact.owner_name.trim() || contact.owner_email
}

/**
 * The project-wide ordering for a list of people: the *rendered* name, then id.
 *
 * Uses the same *key* the backend orders by — the rendered name, i.e.
 * `coalesce(nullif(full_name, ''), email)` as `IdentityCandidateProvider`
 * spells it, which is why `contactLabel` falls back on a blank name and not
 * only on a missing one. It is **not** the same ordering: `localeCompare` is
 * locale-aware, the backend sorts under the database collation, and the two
 * disagree on case and accents. That is tolerable because the order here is
 * cosmetic — this list is read, never indexed into, and no backend result is
 * matched against its positions. Do not "fix" it by reaching for a
 * collation-mimicking comparator; if the order ever becomes load-bearing, the
 * answer is to have the server send it in order.
 *
 * The id tiebreak keeps two people with the same display name in a stable
 * order across renders instead of leaving it to the server's row order.
 */
export function sortContacts(
  contacts: IdentityContactPublic[],
): IdentityContactPublic[] {
  return [...contacts].sort((a, b) => {
    const byName = contactLabel(a).localeCompare(contactLabel(b))
    return byName !== 0 ? byName : a.owner_id.localeCompare(b.owner_id)
  })
}

/**
 * Write the resolved row the server just returned straight into the cache.
 *
 * Both PUT and DELETE answer with the re-resolved `UserChannelPublic`
 * precisely so the client does not have to guess — `user_channels.py` says so
 * in as many words. Invalidating instead would settle the mutation before the
 * refetch lands, leaving a window in which the row is not busy and still
 * renders pre-write state: the switch snaps back, and worse, a second tick in
 * the agent checklist would build its replace-the-whole-list payload from the
 * stale selection and drop the first one.
 *
 * Cancelling the list's in-flight reads is the OTHER HALF of not
 * invalidating, and `useChannelUpdate` does it on every mutate: a refetch
 * that started before the write (a window-focus one, say) lands after it and
 * overwrites this with pre-write data, which is the same dropped tick by a
 * slower route. React Query has no way to know the cache was written by a
 * mutation.
 */
export function writeChannelToCache(
  queryClient: QueryClient,
  updated: UserChannelPublic,
): void {
  queryClient.setQueryData<UserChannelPublic[]>(USER_CHANNELS_KEY, (old) =>
    old?.map((c) => (c.id === updated.id ? updated : c)),
  )
}
