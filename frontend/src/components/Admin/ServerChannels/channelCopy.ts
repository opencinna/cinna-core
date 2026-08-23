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
