/**
 * Credential-type registry — single source of truth for the icon, label,
 * and badge palette associated with each ``CredentialType``. Consumed by
 * the "Add Credential" picker (interactive) and the
 * ``<CredentialTypeBadge>`` component (display-only).
 */
import {
  Briefcase,
  Calendar,
  HardDrive,
  Inbox,
  Key,
  KeyRound,
  KeySquare,
  Mail,
  Network,
  Plug,
  Send,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react"

import type { CredentialType } from "@/client"
import {
  AGENT_API_KEY_LABEL,
  AGENT_API_KEY_TAGLINE,
} from "@/components/Credentials/agentApiKeyCopy"

export interface CredentialTypeOption {
  type: CredentialType
  label: string
  defaultName: string
  keywords: string
  icon: LucideIcon
  /**
   * Opt out of the picker's default "create a row with ``defaultName`` and open
   * its detail page" behaviour, because this type cannot exist as an empty
   * draft: an ``agent_api`` key is bound to a producer agent and a subject user
   * at mint time, so it needs a dialog first. The picker branches on this and
   * ``defaultName`` goes unused for such an entry.
   */
  action?: "agent_api_key"
}

export interface CredentialTypeGroup {
  key: string
  label: string
  // Tailwind classes applied to every badge in this group. Kept as a single
  // concatenated string so the whole palette (bg + text + border + hover) lives
  // in one place per group.
  badgeClass: string
  options: CredentialTypeOption[]
}

export const CREDENTIAL_TYPE_GROUPS: CredentialTypeGroup[] = [
  {
    key: "api_access",
    label: "API & Access",
    badgeClass:
      "bg-slate-100 text-slate-800 border-slate-200 hover:bg-slate-200 dark:bg-slate-800/60 dark:text-slate-100 dark:border-slate-700 dark:hover:bg-slate-700",
    options: [
      {
        type: "api_token",
        label: "API Token",
        defaultName: "API Token",
        keywords: "api token bearer key secret",
        icon: Key,
      },
      {
        type: "ssh_key",
        label: "SSH Key",
        defaultName: "SSH Key",
        keywords: "ssh key git deploy private public",
        icon: KeyRound,
      },
      {
        // Sits here rather than in its own row above the groups: from the
        // outside this is just an API key for external use, not an exceptional
        // kind of access. ``action`` routes it to the mint dialog — it has no
        // empty-draft form. See agentApiKeyCopy.ts for the product name.
        type: "agent_api",
        label: AGENT_API_KEY_LABEL,
        defaultName: AGENT_API_KEY_LABEL,
        keywords: `agent api key external rest curl script ${AGENT_API_KEY_TAGLINE}`,
        icon: KeySquare,
        action: "agent_api_key",
      },
    ],
  },
  {
    key: "email",
    label: "Email",
    badgeClass:
      "bg-amber-50 text-amber-900 border-amber-200 hover:bg-amber-100 dark:bg-amber-950/40 dark:text-amber-100 dark:border-amber-900 dark:hover:bg-amber-900/40",
    options: [
      {
        type: "email_imap",
        label: "Email (IMAP)",
        defaultName: "Email (IMAP)",
        keywords: "email imap mail inbox",
        icon: Inbox,
      },
      {
        type: "email_smtp",
        label: "Email (SMTP)",
        defaultName: "Email (SMTP)",
        keywords: "email smtp mail send outgoing",
        icon: Send,
      },
    ],
  },
  {
    key: "google",
    label: "Google",
    badgeClass:
      "bg-blue-50 text-blue-900 border-blue-200 hover:bg-blue-100 dark:bg-blue-950/40 dark:text-blue-100 dark:border-blue-900 dark:hover:bg-blue-900/40",
    options: [
      {
        type: "gmail_oauth",
        label: "Gmail",
        defaultName: "Gmail",
        keywords: "gmail google oauth mail",
        icon: Mail,
      },
      {
        type: "gmail_oauth_readonly",
        label: "Gmail (Read-Only)",
        defaultName: "Gmail (Read-Only)",
        keywords: "gmail google oauth readonly mail",
        icon: Mail,
      },
      {
        type: "gdrive_oauth",
        label: "Google Drive",
        defaultName: "Google Drive",
        keywords: "google drive files oauth",
        icon: HardDrive,
      },
      {
        type: "gdrive_oauth_readonly",
        label: "Google Drive (Read-Only)",
        defaultName: "Google Drive (Read-Only)",
        keywords: "google drive files oauth readonly",
        icon: HardDrive,
      },
      {
        type: "gcalendar_oauth",
        label: "Google Calendar",
        defaultName: "Google Calendar",
        keywords: "google calendar events oauth",
        icon: Calendar,
      },
      {
        type: "gcalendar_oauth_readonly",
        label: "Google Calendar (Read-Only)",
        defaultName: "Google Calendar (Read-Only)",
        keywords: "google calendar events oauth readonly",
        icon: Calendar,
      },
      {
        type: "google_service_account",
        label: "Google Service Account",
        defaultName: "Google Service Account",
        keywords: "google service account json sa",
        icon: ShieldCheck,
      },
    ],
  },
  {
    key: "applications",
    label: "Applications",
    badgeClass:
      "bg-violet-50 text-violet-900 border-violet-200 hover:bg-violet-100 dark:bg-violet-950/40 dark:text-violet-100 dark:border-violet-900 dark:hover:bg-violet-900/40",
    options: [
      {
        type: "odoo",
        label: "Odoo",
        defaultName: "Odoo",
        keywords: "odoo erp applications",
        icon: Briefcase,
      },
    ],
  },
]

export interface CredentialTypeMeta {
  type: CredentialType | string
  label: string
  icon: LucideIcon
  badgeClass: string
}

// Display overrides — the icon/label/badge used when rendering a credential of
// this type, which is NOT always what the picker offers.
//
// These are applied AFTER the groups above, so for a type appearing in both the
// override wins for display. That is deliberate for ``agent_api``: the picker
// offers "Agent API Key" (the outward-facing half a user mints by hand), while
// *every* agent_api credential — key or auto-created connection — renders under
// one neutral "Agent REST API" badge. A connection is still never created by
// hand; it comes from the "Connect Agent API" helper, which mints the proxy
// token and wires the two agents.
const DISPLAY_ONLY_META: CredentialTypeMeta[] = [
  {
    type: "agent_api",
    label: "Agent REST API",
    icon: Network,
    badgeClass:
      "bg-teal-50 text-teal-900 border-teal-200 hover:bg-teal-100 dark:bg-teal-950/40 dark:text-teal-100 dark:border-teal-900 dark:hover:bg-teal-900/40",
  },
  // mcp_provider connections are created by the "Connect MCP Provider" helper
  // (platform-agent or external-server flow), never by hand.
  {
    type: "mcp_provider",
    label: "MCP Provider",
    icon: Plug,
    badgeClass:
      "bg-indigo-50 text-indigo-900 border-indigo-200 hover:bg-indigo-100 dark:bg-indigo-950/40 dark:text-indigo-100 dark:border-indigo-900 dark:hover:bg-indigo-900/40",
  },
]

const META_BY_TYPE: Map<string, CredentialTypeMeta> = (() => {
  const map = new Map<string, CredentialTypeMeta>()
  for (const group of CREDENTIAL_TYPE_GROUPS) {
    for (const option of group.options) {
      map.set(option.type, {
        type: option.type,
        label: option.label,
        icon: option.icon,
        badgeClass: group.badgeClass,
      })
    }
  }
  for (const meta of DISPLAY_ONLY_META) {
    map.set(meta.type as string, meta)
  }
  return map
})()

const FALLBACK_BADGE_CLASS =
  "bg-slate-50 text-slate-700 border-slate-200 dark:bg-slate-800/60 dark:text-slate-200 dark:border-slate-700"

/**
 * Resolve the display metadata for a credential type. Falls back to a
 * neutral palette and the raw type string when no entry is registered
 * (e.g. for credentials added by future plugins).
 */
export function getCredentialTypeMeta(
  type: CredentialType | string,
): CredentialTypeMeta {
  const hit = META_BY_TYPE.get(type)
  if (hit) return hit
  return {
    type,
    label: type,
    icon: Key,
    badgeClass: FALLBACK_BADGE_CLASS,
  }
}
