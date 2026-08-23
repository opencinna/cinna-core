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
 */
import { type LucideIcon, MessagesSquare, Plug } from "lucide-react"

export interface ChannelConfigField {
  /** Key inside the channel's `config` object. */
  key: string
  label: string
  placeholder?: string
  description?: string
  inputMode?: "numeric" | "text"
  /** Extra validation beyond "not blank". */
  pattern?: { regex: RegExp; message: string }
}

export interface ChannelTypeMeta {
  icon: LucideIcon
  iconClass: string
  /** One line on the picker card — what connecting this actually gets you. */
  tagline: string
  namePlaceholder: string
  /** Empty ⇒ the form renders a raw JSON editor for `config` instead. */
  configFields: ChannelConfigField[]
  secrets: {
    label: string
    placeholder: string
    /** Shown when creating — must say what breaks if it is left blank. */
    helpNew: string
    helpEdit: string
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
}

const CHANNEL_TYPE_META: Record<string, ChannelTypeMeta> = {
  google_chat: GOOGLE_CHAT,
}

export function getChannelTypeMeta(channelType: string): ChannelTypeMeta {
  return CHANNEL_TYPE_META[channelType] ?? FALLBACK
}
