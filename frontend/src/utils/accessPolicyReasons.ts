/**
 * Human copy for the access-policy reason codes the backend returns.
 *
 * The API answers a policy refusal with a bare machine-readable code as the
 * HTTP `detail` — `registration_closed`, `no_admin_google_account`,
 * `invalid_email_pattern:<entry>`. That is deliberate and correct: a code
 * survives translation, log-grepping and a copy rewrite in a way an English
 * sentence does not, and the surface that shows the refusal is the only place
 * that knows who is reading it.
 *
 * But a code only reaches a human through a map, and the codes are raised on
 * four unrelated surfaces — the admin card, the signup form, the password login
 * form and the Google button. A map that lives inside any one of them leaves
 * the other three showing the raw identifier (a toast reading
 * `registration_closed`), or worse, whatever the generated client made up: an
 * `ApiError` for a 403 carries `message === "Forbidden"`, so a Google
 * registration refused because the instance is invite-only, because
 * auto-registration is off, or because the address misses the pattern list all
 * read identically and say nothing. So the map lives here, once, and every
 * surface consumes it.
 *
 * The copy is written for the audience of the surface each code actually
 * appears on: the four registration/auth codes are read by an anonymous visitor
 * being turned away, the rest by an administrator whose save was refused.
 */

/**
 * Reason code → the sentence a human should see.
 *
 * `invalid_email_pattern` is absent on purpose: it is the one composite code,
 * carrying the offending entry after a colon, and its copy is built in
 * {@link accessPolicyReasonCopy}.
 */
const REASON_COPY: Record<string, string> = {
  // ── Registration and sign-in refusals (shown to the visitor) ──────────
  registration_closed:
    "This server is invite-only. Ask an administrator for an invitation, or sign in if you already have an account.",
  email_not_allowed:
    "That email address is not eligible to register on this server. Ask an administrator which addresses are accepted.",
  password_auth_disabled:
    "This server uses Google sign-in. Continue with Google instead of a password.",
  google_auto_register_disabled:
    "Google sign-in does not create accounts on this server. Ask an administrator for an account, then sign in with Google.",

  // ── Admin update refusals (shown to the administrator) ────────────────
  google_oauth_not_configured:
    "Configure Google OAuth before turning off password sign-in — nobody could sign in otherwise.",
  no_admin_google_account:
    "Link a Google account to an administrator before turning off password sign-in. Administrators keep password sign-in as a break-glass path, but no administrator here can currently use Google.",
  no_admin_password:
    "Set a password on an administrator account before turning off password sign-in. Administrators keep password sign-in as a break-glass path for the day Google is unreachable, and no administrator here has a password to fall back on.",
  invalid_registration_mode: "That registration mode is not recognised.",
  invalid_default_user_role:
    "The default role must be Agent User or Agent Developer.",
}

/**
 * Split an error's `detail` into its reason code and, for the one composite
 * code (`invalid_email_pattern:<entry>`), the offending entry.
 *
 * `String.prototype.split(":", 1)` drops the remainder rather than capping the
 * number of splits, and the entry may itself contain a colon, so the boundary
 * is found by hand.
 */
export function parseAccessPolicyReason(error: unknown): {
  code: string
  entry: string | null
} {
  const detail = (error as { body?: { detail?: unknown } } | undefined)?.body
    ?.detail
  const raw = typeof detail === "string" ? detail : ""
  const separator = raw.indexOf(":")
  if (separator === -1) {
    return { code: raw, entry: null }
  }
  return { code: raw.slice(0, separator), entry: raw.slice(separator + 1) }
}

/**
 * The sentence for `error`, or `null` when it is not an access-policy refusal.
 *
 * Null rather than a fallback string so each caller keeps its own error
 * handling for everything else — a duplicate-email 400 and a 422 field error
 * still travel through the handler that already renders them well.
 */
export function accessPolicyReasonCopy(error: unknown): string | null {
  const { code, entry } = parseAccessPolicyReason(error)
  if (!code) return null
  if (code === "invalid_email_pattern") {
    return `"${entry ?? ""}" is not a valid email pattern. Use forms like *@acme.com or *@*.acme.com.`
  }
  return REASON_COPY[code] ?? null
}
