import { Info, type LucideIcon } from "lucide-react"
import type { ReactNode } from "react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/**
 * The house list row (guidelines §3 P3).
 *
 * One entity per row, inside a card or a Sheet. The shape is fixed so that
 * every list in the product reads the same way left to right:
 *
 *   ● name · badge        metadata line          [flags] [action] [On/Off] [⋯]
 *
 * Two things this primitive exists to stop, both of which the hand-written
 * rows it replaced did:
 *
 * 1. **A border per row.** A card is already a bordered box; a border on each
 *    of its five rows draws five more boxes inside it, and the eye counts
 *    boxes before it reads names. Rows are separated by one hairline
 *    (`ListRowGroup`'s `divide-y`) and nothing else. Losing the border also
 *    buys the name ~24px of width, which is what the 1024 yardstick in the
 *    guidelines is always short of.
 * 2. **Enabled/disabled as a `Badge`.** "Active" printed on every live row is
 *    the most repeated and least informative word in a list. It is a 8px dot
 *    at the row's left edge instead — `status` — with the wording moved to a
 *    tooltip and to `sr-only` text.
 *
 * The right cluster's order is not the caller's choice: `flags` are rendered
 * by this component before `children`, so a passive indicator can never end up
 * to the right of the `⋯` menu. See §2 "Row actions" for what may live there.
 */

export type RowStatusTone = "on" | "off" | "warning" | "error"

const STATUS_DOT: Record<RowStatusTone, string> = {
  on: "bg-success",
  off: "bg-muted-foreground/40",
  warning: "bg-warning",
  error: "bg-destructive",
}

const FLAG_TONE: Record<RowStatusTone | "neutral", string> = {
  neutral: "text-muted-foreground",
  on: "text-success",
  off: "text-muted-foreground/60",
  warning: "text-warning",
  error: "text-destructive",
}

export interface RowStatus {
  tone: RowStatusTone
  /** What the dot means, in words: tooltip text and the screen-reader label. */
  label: string
}

interface ListRowGroupProps {
  children: ReactNode
  className?: string
}

/**
 * The separated list a `ListRow` lives in.
 *
 * `-mx-2` lets the hover strip and the hairline bleed to the card's padding
 * while the text itself stays flush with the card's other content, so a list
 * does not read as an inset panel.
 */
export function ListRowGroup({ children, className }: ListRowGroupProps) {
  return (
    <div className={cn("-mx-2 divide-y divide-border", className)}>
      {children}
    </div>
  )
}

interface ListRowProps {
  /** The leading dot. Omit on rows that have no on/off or health state. */
  status?: RowStatus
  /**
   * A small tinted tile before the name, for rows whose entity has an identity
   * worth a glyph (an agent, a provider). Not a place for state — that is
   * `status` — and not for a type that already has a badge.
   */
  icon?: ReactNode
  title: ReactNode
  /** At most one `Badge`; a second fact belongs on the metadata line. */
  badges?: ReactNode
  /**
   * One line, ≤ 2 facts joined by ` · `. **Optional, and expensive:** it adds
   * a line of height to *every* row in the list, so it earns its place only
   * when it is what tells two rows apart. A token prefix beside a named token,
   * a session count on a share link — facts the user does not scan for — go to
   * a flag's tooltip or the editor, and the row stays one line high.
   */
  meta?: ReactNode
  /** Passive indicators (`RowFlag`), rendered left of the action cluster. */
  flags?: ReactNode
  /** The action cluster: ≤ 1 inline action, the On/Off control, the `⋯` menu. */
  children?: ReactNode
  /** Disabled/inactive row: dims everything but keeps the row readable. */
  muted?: boolean
  className?: string
}

export function ListRow({
  status,
  icon,
  title,
  badges,
  meta,
  flags,
  children,
  muted,
  className,
}: ListRowProps) {
  return (
    <div
      className={cn(
        // `group` is always on so a row can hover-reveal a destructive control
        // (P3 hover variant) without each caller re-declaring it.
        "group flex items-center gap-2 px-2 py-1.5 transition-colors hover:bg-muted/40",
        muted && "opacity-60",
        className,
      )}
    >
      {status && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="flex shrink-0 items-center">
              <span
                className={cn("h-2 w-2 rounded-full", STATUS_DOT[status.tone])}
              />
              <span className="sr-only">{status.label}</span>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-xs text-xs">
            {status.label}
          </TooltipContent>
        </Tooltip>
      )}

      {icon && <span className="flex shrink-0 items-center">{icon}</span>}

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{title}</span>
          {badges}
        </div>
        {meta && (
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {meta}
          </p>
        )}
      </div>

      {flags && (
        <div className="flex shrink-0 items-center gap-1.5">{flags}</div>
      )}

      {children && (
        <div className="flex shrink-0 items-center gap-0.5">{children}</div>
      )}
    </div>
  )
}

interface RowFlagProps {
  icon: LucideIcon
  /** What the glyph says. Tooltip text and `sr-only` label — never optional. */
  label: string
  tone?: RowStatusTone | "neutral"
}

/**
 * A passive per-row indicator: a mode, a scope, a kind — a fact the user reads
 * but cannot click.
 *
 * It is an icon rather than a `Badge` because badges cost the name 60–90px
 * each and a list carries the same two or three of them on every row. The
 * default tone is muted: a flag that is coloured on every row is a decoration,
 * so colour is reserved for the flag that means something is off.
 */
export function RowFlag({ icon: Icon, label, tone = "neutral" }: RowFlagProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="flex items-center">
          <Icon className={cn("h-3.5 w-3.5", FLAG_TONE[tone])} />
          <span className="sr-only">{label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-xs">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}

interface RowInfoProps {
  /**
   * The row's secondary facts, one per line. Falsy entries are dropped, so a
   * caller can write the conditional facts inline.
   */
  facts: Array<string | false | null | undefined>
}

/**
 * The row's detail flag: the standard way to carry facts a user wants *about*
 * a row without giving them a line of height on every row of the list.
 *
 * Always an `Info` glyph, always last in the `flags` cluster, so it reads as
 * one affordance across the product: "hover here for the rest". What belongs
 * in it is everything that is true of the row but that nobody scans the list
 * by — a token's prefix and last-use time, a share link's session count, when
 * a thing was created. What does not: the fact that tells two rows apart (that
 * is `title` or `meta`), and anything the user must see without hovering (that
 * is the status dot or a tone-coloured `RowFlag`).
 */
export function RowInfo({ facts }: RowInfoProps) {
  const lines = facts.filter(Boolean) as string[]
  if (lines.length === 0) return null

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="flex items-center">
          <Info className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="sr-only">Details: {lines.join(". ")}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs space-y-0.5 text-xs">
        {lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}
