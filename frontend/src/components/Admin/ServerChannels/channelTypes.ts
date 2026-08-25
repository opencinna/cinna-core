/**
 * Channel-type registry — presentation and config-shape per `channel_type`.
 *
 * The backend registry (`app/services/server_channels/adapters/registry.py`) is
 * the source of truth for *which* types exist; `/channel-types` returns them
 * with a display name. This file adds what the API can't: an icon, a one-line
 * tagline for the picker card, and the config fields that type's adapter
 * expects.
 *
 * A type registered on the backend but missing here is NOT an error: the form
 * falls back to a raw JSON config editor (see `configFields: []`), so a new
 * adapter is administrable the moment it is registered, and gets a typed form
 * whenever someone adds an entry below. That fallback is why the old
 * "hard-coded Google Chat config" hazard is gone — but a typed entry is still
 * the better experience, so add one when you add an adapter.
 *
 * The entries also answer the questions the *rest* of the admin surface used to
 * answer for Google Chat only — what a missing outbound credential is, what a
 * raw destination looks like, whether the sender's identity is verified. Every
 * one of those has a different true answer per transport, so each is a field
 * here rather than a sentence inlined in a component.
 */
import { type LucideIcon, Mail, MessagesSquare, Plug } from "lucide-react"

import type { MailServerType } from "@/client"
import {
  EMAIL_SENDER_SPOOFABLE_WARNING,
  EMAIL_SILENT_DECLINE_NOTE,
} from "./channelCopy"

/**
 * Render a config field as a picker over a resource the admin already manages,
 * instead of asking them to paste its id.
 *
 * Deliberately a small closed union rather than a generic "fetch from here":
 * each kind needs its own query, its own labels and its own empty state, and
 * the form resolves it to a component. Add a member when a new adapter
 * references another admin-owned resource by id.
 */
export type ChannelConfigPicker = {
  kind: "mail_server"
  /** Only servers of this kind are offered — an IMAP id in the SMTP slot is
   *  a save-time 422 from the adapter's reference check. */
  serverType: MailServerType
}

export interface ChannelConfigField {
  /** Key inside the channel's `config` object. */
  key: string
  label: string
  placeholder?: string
  description?: string
  inputMode?: "numeric" | "text"
  /** Extra validation beyond "not blank". */
  pattern?: { regex: RegExp; message: string }
  /** Present ⇒ rendered as a picker instead of a text input. */
  picker?: ChannelConfigPicker
}

export interface ChannelSecretsMeta {
  label: string
  placeholder: string
  /** Shown when creating — must say what breaks if it is left blank. */
  helpNew: string
  helpEdit: string
}

export interface ChannelTypeMeta {
  icon: LucideIcon
  iconClass: string
  /** One line on the picker card — what connecting this actually gets you. */
  tagline: string
  namePlaceholder: string
  /** Empty ⇒ the form renders a raw JSON editor for `config` instead. */
  configFields: ChannelConfigField[]
  /**
   * `null` ⇒ this transport stores no channel secret and the field is not
   * rendered at all.
   *
   * That is a correctness decision, not tidiness. The column is write-only, so
   * a field offered on a transport whose adapter declares
   * `needs_outbound_credentials=False` invites an admin to paste a real
   * credential into a value nothing will ever read — and they get no feedback,
   * because the write succeeds.
   */
  secrets: ChannelSecretsMeta | null
  /**
   * Shown next to the whitelist and auto-registration controls when this
   * transport's sender identity is not verified by anything.
   *
   * Absent for a transport that authenticates its sender (Google Chat signs
   * it), because a warning shown everywhere is read nowhere.
   */
  senderTrustWarning?: string
  /** Extra paragraph in the setup panel, for behaviour the backend's own
   *  `get_setup_instructions` steps can't carry. */
  setupNote?: string
  outboundTest: {
    /** Label for the "type a raw destination instead" option. */
    customTargetLabel: string
    customTargetPlaceholder: string
    /** Shown wherever `has_outbound_credentials` is false — the channel list
     *  badge and the test-outbound control. Must name the *actual* missing
     *  thing for this transport. */
    missingCredentialsHint: string
  }
}

const GOOGLE_CHAT: ChannelTypeMeta = {
  icon: MessagesSquare,
  iconClass: "text-blue-500",
  tagline: "Your team DMs the bot or mentions it in a space.",
  namePlaceholder: "Company Google Chat",
  configFields: [
    {
      // The JWT audience for inbound events. Digits-only is enforced here as
      // well as server-side, because a wrong value fails silently at runtime
      // (every inbound event is rejected) rather than at save time.
      key: "project_number",
      label: "GCP project number",
      placeholder: "123456789012",
      inputMode: "numeric",
      description:
        "The numeric project number of your Chat app — used as the webhook JWT audience.",
      pattern: {
        regex: /^\d+$/,
        message: "Must be the numeric GCP project number, not the project ID",
      },
    },
  ],
  secrets: {
    label: "Service account JSON",
    placeholder: '{ "type": "service_account", ... }',
    helpNew:
      "Used to post replies back into the chat. You can add it later, but until you do the agent's replies won't be delivered. Encrypted at rest and never shown again.",
    helpEdit:
      "Leave blank to keep the stored credential. It's encrypted at rest and never shown again.",
  },
  outboundTest: {
    customTargetLabel: "Custom space or thread ID…",
    customTargetPlaceholder: "spaces/AAAA",
    missingCredentialsHint:
      "No service account key stored, so the agent's replies can't be delivered. Edit the channel to add one.",
  },
}

/** A plain address, no display name: it becomes the SMTP envelope sender. */
const BARE_EMAIL = {
  regex: /^[^\s@<>,]+@[^\s@<>,]+$/,
  message:
    "Must be a plain address like support@corp.com, with no display name",
}

const EMAIL: ChannelTypeMeta = {
  icon: Mail,
  iconClass: "text-emerald-500",
  tagline: "People write to a mailbox you poll, and the agent writes back.",
  namePlaceholder: "Support Mailbox",
  configFields: [
    {
      // Pickers rather than id fields: both values are `mail_server_config`
      // rows the admin manages on the tab next to this one, and the adapter
      // rejects an id of the wrong kind at save time. Filtering the list to
      // the right kind turns that 422 into a choice that cannot be made.
      key: "incoming_server_id",
      label: "Incoming mail server (IMAP)",
      description: "Polled on a timer for new mail. No inbound URL is used.",
      picker: { kind: "mail_server", serverType: "imap" },
    },
    {
      key: "outgoing_server_id",
      label: "Outgoing mail server (SMTP)",
      description:
        "Sends the agent's replies. This server's stored password is the channel's outbound credential — nothing is stored on the channel itself.",
      picker: { kind: "mail_server", serverType: "smtp" },
    },
    {
      key: "incoming_mailbox",
      label: "Polled mailbox",
      placeholder: "support@corp.com",
      description:
        "The address people write to. Mail addressed to anyone else in the same inbox is ignored, so one IMAP account can serve several channels.",
      pattern: BARE_EMAIL,
    },
    {
      key: "from_address",
      label: "Reply from",
      placeholder: "support@corp.com",
      description: "The From: address the agent's answers are sent with.",
      pattern: BARE_EMAIL,
    },
  ],
  // `needs_outbound_credentials=False` on the adapter: the SMTP server row is
  // the credential. See `ChannelTypeMeta.secrets`.
  secrets: null,
  senderTrustWarning: EMAIL_SENDER_SPOOFABLE_WARNING,
  setupNote: EMAIL_SILENT_DECLINE_NOTE,
  outboundTest: {
    // The transport-facing thread key on this channel is the thread's root
    // Message-ID, brackets included.
    customTargetLabel: "Custom Message-ID…",
    customTargetPlaceholder: "<CAF...@mail.example.com>",
    missingCredentialsHint:
      "No outgoing mail server selected, so the agent's replies can't be sent. Edit the channel and pick an SMTP server.",
  },
}

/** Used for a backend type with no entry above. Deliberately generic. */
const FALLBACK: ChannelTypeMeta = {
  icon: Plug,
  iconClass: "text-muted-foreground",
  tagline: "Registered on this server. Configure it as raw JSON.",
  namePlaceholder: "Channel name",
  configFields: [],
  secrets: {
    label: "Credentials",
    placeholder: "Paste the credential this channel needs",
    helpNew:
      "Whatever this channel needs to send replies. Encrypted at rest and never shown again.",
    helpEdit:
      "Leave blank to keep the stored credential. It's encrypted at rest and never shown again.",
  },
  outboundTest: {
    customTargetLabel: "Custom thread ID…",
    customTargetPlaceholder: "Thread or conversation ID",
    missingCredentialsHint:
      "No outbound credential stored, so the agent's replies can't be delivered. Edit the channel to add one.",
  },
}

const CHANNEL_TYPE_META: Record<string, ChannelTypeMeta> = {
  google_chat: GOOGLE_CHAT,
  email: EMAIL,
}

export function getChannelTypeMeta(channelType: string): ChannelTypeMeta {
  return CHANNEL_TYPE_META[channelType] ?? FALLBACK
}
