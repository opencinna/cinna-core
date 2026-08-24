/**
 * Shared copy and helpers for the Channels admin tab.
 *
 * The whitelist wording lives here rather than inline because it is the single
 * most misunderstandable control in this feature: an admin who assumes an empty
 * box means "open to everyone" has made a security mistake, not a config
 * mistake. The backend fails closed; the UI has to say so in words.
 */

export const WHITELIST_HELP =
  "Comma-separated patterns, e.g. *@example.com, devops.*@support.com"

/** Deliberately blunt. Empty is the dangerous-looking-but-safe default. */
export const WHITELIST_EMPTY_WARNING =
  "Empty means nobody — no one can reach your agents through this channel."

export const WHITELIST_WILDCARD_WARNING =
  "* allows anyone whose identity the channel verifies, including people outside your organisation."

export const AUTO_REGISTER_HELP =
  "Creates a passwordless account for a whitelisted sender the first time they message. The whitelist above is the only gate — the sign-up domain allowlist is not re-checked."

/** Why a bundle on the auto-install list still won't be installed. */
export const VISIBILITY_WARNING =
  "Not public: external users can't install this until the bundle is made public or granted to them. It will simply never be offered."

export const NO_TRIGGER_PROMPT_WARNING =
  "No router trigger prompt on the latest revision, so routing can never match this bundle. Publish a revision with one."

/**
 * Split a whitelist string the way the backend matcher does.
 *
 * Must stay in lockstep with `match_email_pattern`
 * (`backend/app/services/common/email_patterns.py`), which splits on commas,
 * trims, lowercases, drops blanks, and allows the sender if ANY token matches.
 *
 * Getting this wrong is a security-comms failure, not a cosmetic one: comparing
 * the raw string to `"*"` means `"*, ops@corp.com"` — a blanket allow — renders
 * as a normal-looking scoped list, and the admin walks away believing they
 * restricted access. Tokenizing catches every spelling of "allow everyone".
 */
export function parseWhitelist(value: string): {
  tokens: string[]
  isEmpty: boolean
  hasWildcard: boolean
} {
  const tokens = value
    .split(",")
    .map((p) => p.trim().toLowerCase())
    .filter(Boolean)
  return {
    tokens,
    // "  ,  " has no usable tokens — empty to the backend, so empty here too.
    isEmpty: tokens.length === 0,
    hasWildcard: tokens.some(isMatchEverything),
  }
}

/**
 * True for a token that fnmatch treats as "match any address".
 *
 * Not just the literal `*`. Under fnmatch:
 *   - `**`, `*?`, `?*`, `***` — any run of `*`/`?` with at least one `*` —
 *     match every address, so `"**, ops@corp.com"` is a blanket allow;
 *   - `*@*` matches anything containing an `@`, which for email addresses is
 *     every address in practice (likewise `*@`, `@*`).
 *
 * Warning only on `"*"` would render both as ordinary scoped lists — the
 * "approximately-right warning is worse than none" failure this control
 * exists to avoid.
 *
 * Implemented as: strip every `*`/`?`; if what remains is nothing (or just
 * the `@` separator) AND the token contained at least one `*`, it constrains
 * nothing. That keeps `*@example.com` (leaves `@example.com`) and `???`
 * (length-limited, no `*`) correctly out.
 */
function isMatchEverything(token: string): boolean {
  if (!token.includes("*")) return false
  const literal = token.replace(/[*?]/g, "")
  return literal === "" || literal === "@"
}

// ---------------------------------------------------------------------------
// Availability policy (admin-owned defaults)
//
// These four controls are *defaults*, not settings: they describe what happens
// to a user who has never opened Settings → Channels. Every label below has to
// carry that, because "Enabled for users" reads as a switch that turns the
// channel on for everybody — and it is not one. A user who has set their own
// value keeps it.
// ---------------------------------------------------------------------------

export const VISIBILITY_PUBLIC = "public"
export const VISIBILITY_RESTRICTED = "restricted"

export const AGENT_SCOPE_ALL = "all"
export const AGENT_SCOPE_LIST = "list"
export const AGENT_SCOPE_NONE = "none"

// The values a channel that has never been edited carries. They are the
// backend model's own defaults (`ServerChannelBase`), and they are what the
// *create* form starts from — deliberately NOT the coercion helpers below,
// which answer a different question ("this server sent me a value I do not
// recognise") and answer it in the fail-closed direction. Running a blank
// create form through them would open every new channel as `restricted` /
// `none` and quietly disagree with what the API would have defaulted to.
export const NEW_CHANNEL_VISIBILITY = VISIBILITY_PUBLIC
export const NEW_CHANNEL_AGENT_SCOPE = AGENT_SCOPE_ALL

/**
 * Coerce a **stored** value into one the segmented control can render.
 *
 * `visibility` is a plain VARCHAR on purpose (new values need no migration),
 * so the form has to cope with a string it does not know. It resolves the same
 * way the backend does — `ChannelPolicyService.describe` treats anything that
 * is not exactly `"public"` as restricted — which is the fail-closed
 * direction: an unrecognised value must never render as, and then be saved
 * back as, "everyone".
 */
export function asVisibility(value: string): string {
  return value === VISIBILITY_PUBLIC ? VISIBILITY_PUBLIC : VISIBILITY_RESTRICTED
}

/**
 * Same idea for the scope, and it narrows rather than widens.
 *
 * This mirrors `ChannelPolicyService._normalise_scope`, which maps anything
 * that is not exactly `"all"` or `"list"` onto `"none"`. Getting the direction
 * wrong here is not cosmetic: `onSubmit` always sends `default_agent_scope`,
 * so a value this client did not recognise would render as "All their agents"
 * and then be written back as `"all"` the next time an admin saved an
 * unrelated field — silently widening every inheriting user's default from
 * "nothing routes" to "every agent the sender owns".
 */
export function asAgentScope(value: string): string {
  if (value === AGENT_SCOPE_ALL) return AGENT_SCOPE_ALL
  if (value === AGENT_SCOPE_LIST) return AGENT_SCOPE_LIST
  return AGENT_SCOPE_NONE
}

export const VISIBILITY_OPTIONS = [
  {
    value: VISIBILITY_PUBLIC,
    label: "Everyone",
    description: "Every platform user can use this channel.",
  },
  {
    value: VISIBILITY_RESTRICTED,
    label: "Chosen users",
    description: "Only the users you grant below can use this channel.",
  },
] as const

export const AGENT_SCOPE_OPTIONS = [
  {
    value: AGENT_SCOPE_ALL,
    label: "All their agents",
    description: "Anything the sender owns can be picked by the router.",
  },
  {
    value: AGENT_SCOPE_LIST,
    label: "Only ones they pick",
    description:
      "The list lives in each user's own settings and starts empty, so nothing routes for a user until they pick.",
  },
  {
    value: AGENT_SCOPE_NONE,
    label: "None",
    description:
      "Nothing routes until a user switches themselves to a chosen list.",
  },
] as const

export const VISIBILITY_HELP =
  "Who may use this channel at all. Being granted does not switch it on for them — that is the default below."

export const DEFAULT_ENABLED_HELP =
  "What a user who has never opened Settings → Channels gets. Changing this follows through to everyone who has not set their own value; anyone who has keeps theirs."

export const DEFAULT_AGENT_SCOPE_HELP =
  "Which of the sender's own agents the router may consider by default. Users can narrow this to a chosen list in their own settings."

export const ALLOW_AUTO_INSTALL_HELP =
  "Lets the router install a bundle from the auto-install catalog when none of the sender's own agents match. Off means an unmatched message simply gets no answer."

/** Restricted with nobody on the list is a channel nobody can use. */
export const NO_GRANTS_WARNING =
  "No users granted yet — while this channel is restricted, nobody can use it."
