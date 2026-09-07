import { Loader2, Users } from "lucide-react"

import type { IdentityContactPublic } from "@/client"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { contactLabel } from "./channelScopes"

/**
 * The consent sentence that goes with these rows, in both hosts.
 *
 * WHICH WAY THE PER-PERSON SWITCH POINTS
 * --------------------------------------
 * It is the **caller's** switch, not the receiver's: the row is keyed by
 * `target_user_id`, and `IdentityService.toggle_identity_contact` filters on
 * `target_user_id == current_user.id`. So it answers "may I address this
 * person?", and turning it off removes them from the reader's OWN reach — it
 * does not stop them reaching the reader. Nothing on this tab lets a user
 * control who may reach them; that is the identity owner's `is_active` on the
 * binding and the assignment, edited from the identity-sharing screens.
 * Getting this sentence backwards is not a copy nit: this is the one control
 * here whose entire purpose is informed consent, and a user who reads it as a
 * shield will leave it on for exactly the person they meant to block. It stays
 * on the surface, not in a tooltip.
 */
export function IdentityConsentFootnote() {
  return (
    <p className="text-xs text-muted-foreground">
      These switches decide who{" "}
      <span className="font-medium text-foreground">you</span> can address by
      name — they do not control who can reach you. They are per person, not per
      channel, so turning someone off here also stops you addressing them
      anywhere else identity is used.
    </p>
  )
}

interface IdentityContactRowProps {
  contact: IdentityContactPublic
  /** A write for this person is in flight. */
  isPending: boolean
  onToggle: (isEnabled: boolean) => void
}

/**
 * One person who has shared an agent with this user, as a compact P3 row.
 *
 * Rendered by both the card and its "Show all" Sheet. The row's purpose is the
 * switch, so the switch is its one inline action and there is no `⋯` menu —
 * there is no second action to put in one.
 */
export function IdentityContactRow({
  contact,
  isPending,
  onToggle,
}: IdentityContactRowProps) {
  const label = contactLabel(contact)

  return (
    <div
      className={cn(
        "flex items-center justify-between px-3 py-2 border rounded-lg",
        !contact.is_enabled && "opacity-60",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md shrink-0 flex items-center justify-center bg-muted">
            <Users className="h-3.5 w-3.5 text-muted-foreground" />
          </div>
          <span className="text-sm font-medium truncate min-w-0">{label}</span>
        </div>
        <p className="text-xs text-muted-foreground truncate mt-0.5">
          {/* The email is shown alongside the name because "which Alex is
              this?" has to be answerable before deciding to let them read a
              conversation. It is skipped when it IS the name, to avoid a row
              that says the same thing twice. */}
          {label !== contact.owner_email && `${contact.owner_email} · `}
          {contact.agent_count === 1
            ? "1 agent"
            : `${contact.agent_count} agents`}
        </p>
      </div>

      <div className="flex items-center gap-0.5 shrink-0">
        {/* A reserved slot: the spinner must not push the switch sideways. */}
        <span className="flex h-3.5 w-3.5 items-center justify-center">
          {isPending && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          )}
        </span>
        <Switch
          checked={contact.is_enabled}
          disabled={isPending}
          aria-label={`Let me address ${label} by name`}
          onCheckedChange={onToggle}
        />
      </div>
    </div>
  )
}
