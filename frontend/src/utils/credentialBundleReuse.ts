import type { CredentialType } from "@/client"

/**
 * Which credential types an owner may let recipients re-use inside the
 * bundles and skills they publish. Mirrors ``BUNDLE_REUSABLE_TYPES`` in the
 * backend ``credential_reuse_policy`` (connections only). Restricted external
 * keys cannot be shared, so they never reach the surfaces that ask. A Record
 * so a new backend type fails the typecheck here until someone decides.
 */
const BUNDLE_REUSABLE: Record<CredentialType, boolean> = {
  email_imap: false,
  email_smtp: false,
  odoo: false,
  gmail_oauth: false,
  gmail_oauth_readonly: false,
  gdrive_oauth: false,
  gdrive_oauth_readonly: false,
  gcalendar_oauth: false,
  gcalendar_oauth_readonly: false,
  gmeet_oauth: false,
  gmeet_oauth_readonly: false,
  google_service_account: false,
  api_token: false,
  ssh_key: false,
  agent_api: true,
  mcp_provider: false,
}

export function isBundleReusableCredentialType(type: CredentialType): boolean {
  return BUNDLE_REUSABLE[type] ?? false
}

/**
 * Why a publisher may not embed a credential, from the backend's
 * ``credential_reuse_policy`` reason code. `null` for a code this build does
 * not know, so the caller renders nothing rather than a raw code.
 *
 * @param owner the owner's display (email) — used for a credential shared
 *   with the publisher.
 * @param isOwn the publisher owns the credential.
 */
export function reuseBlockedSentence(
  reason: string | null | undefined,
  owner: string | null | undefined,
  isOwn: boolean,
): string | null {
  const who = owner || "another person"
  switch (reason) {
    case "sharing_off":
      return isOwn
        ? "Sharing is off for this credential; turn it on to embed it."
        : `Owned by ${who}, who has turned sharing off.`
    case "reuse_off":
      return `Owned by ${who}, who has not allowed re-use inside bundles.`
    case "type_not_reusable":
      return `Owned by ${who}; this kind of credential cannot be re-used inside bundles.`
    case "no_share_path":
      return `Owned by ${who}, who no longer shares it with you.`
    case "not_owned":
      return `Owned by ${who}; only they can embed it.`
    default:
      return null
  }
}
