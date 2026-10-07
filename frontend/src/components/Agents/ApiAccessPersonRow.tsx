import { KeyRound, Loader2, Lock, Pencil, ShieldCheck, Trash2 } from "lucide-react"

import type { AgentApiAccessEntry } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  apiAccessPersonLabel,
  pluralize,
  summarizeApiAccessPerson,
} from "./apiAccess"

interface ApiAccessPersonRowProps {
  entry: AgentApiAccessEntry
  /** Issuing is refused while the producer's external access is off. */
  canGiveKey: boolean
  /** Per-user scopes are on — otherwise editing them would do nothing. */
  scopesEnabled: boolean
  /** A write on this row is in flight — its name, for the spinner. */
  pendingLabel?: string | null
  onOpen: () => void
  onGiveKey: () => void
  onEditScopes: () => void
  onRemoveScopes: () => void
}

/**
 * One person with access to this producer's API — the S1 row, shared by the
 * card and the "Show all" Sheet so the two cannot drift.
 *
 * **The row is the Details control** (§2 Row as the control): click, Enter or
 * Space opens that person's keys (S1a), where a key is revoked. The `⋯` holds
 * the person-level verbs: another key, their scopes.
 *
 * Dot = the best key (working → paused → all ended); a person who only has
 * scopes and no key has no dot. `meta` is email and live-key count — what
 * tells two people apart; the rest is the `RowInfo` tooltip.
 */
export function ApiAccessPersonRow({
  entry,
  canGiveKey,
  scopesEnabled,
  pendingLabel,
  onOpen,
  onGiveKey,
  onEditScopes,
  onRemoveScopes,
}: ApiAccessPersonRowProps) {
  const name = apiAccessPersonLabel(entry)
  const scopes = entry.scopes ?? []
  const s = summarizeApiAccessPerson(entry)
  const live = s.liveKeys.length
  const ended = s.endedKeys.length

  const meta = [
    entry.user.full_name && entry.user.email ? entry.user.email : null,
    live > 0 ? pluralize(live, "key", "keys") : null,
  ]
    .filter(Boolean)
    .join(" · ")

  const lastUsed = s.lastUsedAt ? formatRelativeTimestamp(s.lastUsedAt) : null
  const hasKeys = live + ended > 0

  // Only a click that lands in this row's own DOM opens Details: the menu's
  // content and the dialogs are portals whose React events still bubble up
  // here. A click on the `⋯` trigger is the menu's. And not while a write on
  // this row is in flight — the disabled trigger lets the click fall through.
  const openDetails = (e: React.SyntheticEvent) => {
    if (pendingLabel) return
    if (!e.currentTarget.contains(e.target as Node)) return
    if ((e.target as HTMLElement).closest("button")) return
    onOpen()
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: `ListRow` renders `div`s, which a real `<button>` cannot contain — the same trade the addon rows make.
    <div
      role="button"
      tabIndex={0}
      aria-label={`API access details for ${name}`}
      className="cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      onClick={openDetails}
      onKeyDown={(e) => {
        if (e.key !== "Enter" && e.key !== " ") return
        if (pendingLabel) return
        // Only the row itself: Enter on the `⋯` trigger is the menu's.
        if (e.target !== e.currentTarget) return
        e.preventDefault()
        onOpen()
      }}
    >
      <ListRow
        status={s.status}
        muted={s.allEnded}
        title={name}
        meta={meta || undefined}
        flags={
          <>
            {scopes.length > 0 && (
              <RowFlag
                icon={ShieldCheck}
                label={`Scopes: ${scopes.join(", ")}`}
              />
            )}
            {s.anyLiveReadOnly && <RowFlag icon={Lock} label="Read-only key" />}
            <RowInfo
              facts={[
                hasKeys && (lastUsed ? `Last used ${lastUsed}` : "Never used"),
                ended > 0 && pluralize(ended, "ended key", "ended keys"),
                s.linkedAgentCount > 0 &&
                  `Linked to ${pluralize(s.linkedAgentCount, "agent", "agents")}`,
              ]}
            />
          </>
        }
      >
        {pendingLabel ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className="flex h-7 w-7 items-center justify-center"
                aria-live="polite"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                <span className="sr-only">{pendingLabel}</span>
              </span>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              {pendingLabel}
            </TooltipContent>
          </Tooltip>
        ) : (
          <RowActionsMenu label={`${name}'s API access`}>
            <DropdownMenuItem disabled={!canGiveKey} onSelect={onGiveKey}>
              <KeyRound />
              Give another key
            </DropdownMenuItem>
            {scopesEnabled && (
              <DropdownMenuItem onSelect={onEditScopes}>
                <Pencil />
                Edit scopes
              </DropdownMenuItem>
            )}
            {entry.grant_id && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem variant="destructive" onSelect={onRemoveScopes}>
                  <Trash2 />
                  Remove scopes
                </DropdownMenuItem>
              </>
            )}
          </RowActionsMenu>
        )}
      </ListRow>
    </div>
  )
}
