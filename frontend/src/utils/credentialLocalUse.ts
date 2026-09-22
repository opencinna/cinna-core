import type { CredentialType } from "@/client"

/**
 * Which credential types work on a recipient's computer out of the box, so
 * Desktop delivery may list and copy them. Mirrors
 * ``LOCALLY_COMPATIBLE_TYPES`` in the backend ``desktop_credential_service``.
 * Everything else is never delivered: OAuth types (refreshing the token needs
 * the OAuth client secret private to the server), ssh_key (the private key
 * never leaves Core), agent_api and mcp_provider. A Record so a new backend
 * type fails the typecheck here until someone decides.
 */
const LOCALLY_DELIVERABLE: Record<CredentialType, boolean> = {
  email_imap: true,
  email_smtp: true,
  odoo: true,
  gmail_oauth: false,
  gmail_oauth_readonly: false,
  gdrive_oauth: false,
  gdrive_oauth_readonly: false,
  gcalendar_oauth: false,
  gcalendar_oauth_readonly: false,
  google_service_account: true,
  api_token: true,
  ssh_key: false,
  agent_api: false,
  mcp_provider: false,
}

export function isLocallyDeliverableCredentialType(
  type: CredentialType,
): boolean {
  return LOCALLY_DELIVERABLE[type] ?? false
}
