import { useQuery } from "@tanstack/react-query"
import { Bot, UserCheck, Users } from "lucide-react"
import { type ReactNode, useEffect, useRef, useState } from "react"

import { CredentialsService } from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import type { CredentialRowModel } from "@/components/Credentials/credentialColumns"
import { getCredentialTypeMeta } from "@/components/Credentials/credentialTypes"
import { Badge } from "@/components/ui/badge"
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover"
import { Skeleton } from "@/components/ui/skeleton"
import { pluralize } from "@/utils/pluralize"
import { peopleAndGroups } from "@/utils/sharedWith"

/** Names shown in a hover before the rest collapse into "and N more". */
const HOVER_LIMIT = 5

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

const HOVER_OPEN_DELAY = 200
const HOVER_CLOSE_DELAY = 150

/**
 * A badge that opens a detail card two ways:
 * - **hover** (mouse only): opens after a short delay, focus stays where it
 *   is, and it closes once the pointer has left both the badge and the card;
 * - **click / Enter / Space / tap**: opens and moves focus into the card, so
 *   its Retry is reachable from the keyboard; Escape closes it and puts focus
 *   back on the badge.
 * A `Popover` rather than a HoverCard, whose content is never in the tab
 * order. The badge is a real `<button>`, so the table's row click ignores it,
 * and the card is portaled, so the row's `contains` guard ignores clicks in
 * it. The card's body mounts only while open: a query inside it runs the
 * first time someone looks, not once per row.
 */
function DetailBadge({
  label,
  children,
  detail,
}: {
  /** What the badge says to a screen reader: "Shared with 3 people". */
  label: string
  children: ReactNode
  detail: ReactNode
}) {
  const [open, setOpen] = useState(false)
  // How the card was opened decides whether it takes focus and whether
  // leaving with the pointer closes it.
  const [openedBy, setOpenedBy] = useState<"hover" | "click">("hover")
  const buttonRef = useRef<HTMLButtonElement>(null)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const returnFocus = useRef(false)

  const clearTimer = () => clearTimeout(timer.current)
  // A pending hover timer must not fire after the row unmounts.
  useEffect(() => () => clearTimeout(timer.current), [])

  const hoverOpen = () => {
    clearTimer()
    if (open) return
    timer.current = setTimeout(() => {
      setOpenedBy("hover")
      setOpen(true)
    }, HOVER_OPEN_DELAY)
  }
  const hoverClose = () => {
    clearTimer()
    if (openedBy !== "hover") return
    timer.current = setTimeout(() => setOpen(false), HOVER_CLOSE_DELAY)
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        clearTimer()
        setOpen(next)
      }}
    >
      <PopoverAnchor asChild>
        <button
          ref={buttonRef}
          type="button"
          aria-label={label}
          aria-haspopup="dialog"
          aria-expanded={open}
          onPointerEnter={(e) => {
            if (e.pointerType === "mouse") hoverOpen()
          }}
          onPointerLeave={(e) => {
            if (e.pointerType === "mouse") hoverClose()
          }}
          onClick={() => {
            // Opens only: hover has usually opened it already, and a toggle
            // would shut the card the user is reaching for.
            clearTimer()
            setOpenedBy("click")
            setOpen(true)
          }}
          className="inline-flex shrink-0 rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          <Badge variant="outline" className="h-5 gap-1 tabular-nums">
            {children}
          </Badge>
        </button>
      </PopoverAnchor>
      <PopoverContent
        align="end"
        className="w-72 p-3 text-sm"
        onPointerEnter={clearTimer}
        onPointerLeave={hoverClose}
        onOpenAutoFocus={(e) => {
          // A hover must not pull focus out of wherever the user is.
          if (openedBy === "hover") e.preventDefault()
        }}
        onEscapeKeyDown={() => {
          returnFocus.current = true
        }}
        onCloseAutoFocus={(e) => {
          // The badge is an anchor, not the Popover's trigger, so Radix has
          // nothing to return focus to; do it for Escape only, and leave an
          // outside click's focus where the click put it.
          e.preventDefault()
          if (returnFocus.current) buttonRef.current?.focus()
          returnFocus.current = false
        }}
      >
        {detail}
      </PopoverContent>
    </Popover>
  )
}

/**
 * A fixed place for one count, kept when the count is absent: the position
 * says which fact it is, so a share "2" and an agent "2" never take turns in
 * the same spot, and the type badges end on one edge.
 */
function CountSlot({ children }: { children?: ReactNode }) {
  return (
    <span className="hidden w-12 shrink-0 justify-end lg:flex">{children}</span>
  )
}

function DetailTitle({ children }: { children: ReactNode }) {
  return <p className="mb-2 text-xs font-medium">{children}</p>
}

function DetailLoading() {
  return (
    <div className="space-y-1.5">
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
    </div>
  )
}

function MoreLine({ total }: { total: number }) {
  if (total <= HOVER_LIMIT) return null
  return (
    <p className="mt-1.5 text-xs text-muted-foreground">
      and {total - HOVER_LIMIT} more — open the credential for all of them
    </p>
  )
}

/**
 * Who an owned credential is shared with: people, then user groups. Both
 * owner-only endpoints; each is fetched only when its count says it exists.
 */
function SharesDetail({
  credentialId,
  hasPeople,
  hasGroups,
}: {
  credentialId: string
  hasPeople: boolean
  hasGroups: boolean
}) {
  const people = useQuery({
    queryKey: ["credential-shares", credentialId],
    // Opening a card again within half a minute does not refetch.
    staleTime: 30_000,
    queryFn: () => CredentialsService.getCredentialShares({ credentialId }),
    enabled: hasPeople,
  })
  const groups = useQuery({
    queryKey: ["credential-group-shares", credentialId],
    staleTime: 30_000,
    queryFn: () =>
      CredentialsService.listCredentialGroupShares({ credentialId }),
    enabled: hasGroups,
  })
  const parts = [hasPeople && people, hasGroups && groups].filter(
    (q) => q !== false,
  )

  // Retry keeps `isError` until it succeeds; show the wait, not the error.
  if (parts.some((q) => q.data === undefined && q.isFetching)) {
    return <DetailLoading />
  }
  const failed = parts.find((q) => q.isError && q.data === undefined)
  if (failed) {
    return (
      <QueryErrorAlert
        error={failed.error}
        fallback="Couldn't load who it's shared with"
        onRetry={() => {
          for (const q of parts) q.refetch()
        }}
        compact
      />
    )
  }
  if (parts.some((q) => !q.data)) return <DetailLoading />

  // People first, groups after them (S17).
  const rows = [
    ...(people.data?.data ?? []).map((share) => ({
      key: share.id,
      label: share.shared_with_email,
      detail: formatRelativeTimestamp(share.shared_at) || "",
      group: false,
    })),
    ...(groups.data?.data ?? []).map((g) => ({
      key: `group:${g.group_id}`,
      label: g.group_name,
      detail: pluralize(g.member_count ?? 0, "member", "members"),
      group: true,
    })),
  ]

  return (
    <>
      <DetailTitle>Shared with</DetailTitle>
      <ul className="space-y-1">
        {rows.slice(0, HOVER_LIMIT).map((row) => (
          <li key={row.key} className="flex min-w-0 items-baseline gap-2">
            {row.group && (
              <Users
                className="h-3 w-3 shrink-0 self-center text-muted-foreground"
                aria-label="Group"
              />
            )}
            <span className="min-w-0 truncate">{row.label}</span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {row.detail}
            </span>
          </li>
        ))}
      </ul>
      <MoreLine total={rows.length} />
    </>
  )
}

/**
 * Which of the owner's agents use the credential. Read from the deletion
 * impact (owner-only), under the key the delete dialog and the Sharing card
 * already use, so opening one warms the others.
 */
function AgentsDetail({ credentialId }: { credentialId: string }) {
  const { data, isError, isFetching, error, refetch } = useQuery({
    queryKey: ["credential-deletion-impact", credentialId],
    // Opening a card again within half a minute does not refetch.
    staleTime: 30_000,
    queryFn: () =>
      CredentialsService.getCredentialDeletionImpact({ id: credentialId }),
  })

  // Retry keeps `isError` until it succeeds; show the wait, not the error.
  if (data === undefined && isFetching) return <DetailLoading />
  if (isError && data === undefined) {
    return (
      <QueryErrorAlert
        error={error}
        fallback="Couldn't load which agents use it"
        onRetry={() => refetch()}
        compact
      />
    )
  }
  if (!data) return <DetailLoading />

  const agents = data.affected_own_agents ?? []
  return (
    <>
      <DetailTitle>Used by</DetailTitle>
      <div className="flex flex-wrap gap-1.5">
        {agents.slice(0, HOVER_LIMIT).map((agent) => (
          <AgentBadge key={agent.id} agent={agent} />
        ))}
      </div>
      <MoreLine total={agents.length} />
    </>
  )
}

/**
 * The row's facts after the name, right-aligned beside the `⋯`: type, sharing,
 * agents. Type is a badge at the user's request (it is the one on every row). Absent facts render nothing (§2 Absent facts) — only the type is on
 * every row. Below `lg` only the type stays: at 768–1023 beside the sidebar
 * the counts would leave the name ~20px.
 */
export function CredentialRowBadges({
  credential,
}: {
  credential: CredentialRowModel
}) {
  const typeLabel = getCredentialTypeMeta(credential.type).label
  const shareCount = credential.share_count ?? 0
  const groupShareCount = credential.group_share_count ?? 0
  const viaGroups = credential.via_groups ?? []
  const agentCount = credential.agent_usage_count
  const owner = credential.owner_email
  const sharedWhen = credential.shared_at
    ? formatRelativeTimestamp(credential.shared_at)
    : null

  return (
    <div className="flex min-w-0 items-center justify-end gap-1.5">
      {/* First, then one fixed slot per count (see CountSlot). */}
      <Badge
        variant="secondary"
        className="h-5 min-w-0 max-w-[200px] shrink justify-start"
      >
        <span className="truncate">{typeLabel}</span>
      </Badge>
      <CountSlot>
        {credential.is_shared
          ? owner && (
              <DetailBadge
                label={`Shared with you by ${owner}`}
                detail={
                  <>
                    <p className="break-words">
                      Shared with you by {owner}
                      {sharedWhen ? ` · ${sharedWhen}` : ""}
                    </p>
                    {viaGroups.length > 0 && (
                      <p className="mt-1 break-words text-muted-foreground">
                        Through the{" "}
                        {viaGroups.length === 1 ? "group" : "groups"}{" "}
                        {viaGroups.join(", ")}
                      </p>
                    )}
                  </>
                }
              >
                <UserCheck className="h-3 w-3" aria-hidden="true" />
              </DetailBadge>
            )
          : shareCount + groupShareCount > 0 && (
              <DetailBadge
                label={`Shared with ${peopleAndGroups(shareCount, groupShareCount)}`}
                detail={
                  <SharesDetail
                    credentialId={credential.id}
                    hasPeople={shareCount > 0}
                    hasGroups={groupShareCount > 0}
                  />
                }
              >
                <Users className="h-3 w-3" aria-hidden="true" />
                {shareCount + groupShareCount}
              </DetailBadge>
            )}
      </CountSlot>
      <CountSlot>
        {agentCount > 0 && (
          <DetailBadge
            label={`Used by ${plural(agentCount, "agent", "agents")}`}
            detail={
              credential.is_shared ? (
                // The agent list is owner-only; a recipient gets the count of
                // their own agents, which is what the badge counts.
                <p>Used by {agentCount} of your agents</p>
              ) : (
                <AgentsDetail credentialId={credential.id} />
              )
            }
          >
            <Bot className="h-3 w-3" aria-hidden="true" />
            {agentCount}
          </DetailBadge>
        )}
      </CountSlot>
    </div>
  )
}
