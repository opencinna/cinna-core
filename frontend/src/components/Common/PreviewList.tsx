import { Fragment, type ReactNode } from "react"

import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

/**
 * The guidelines' cap on rows in a card (§2 "List inside a card"). It is a
 * house rule rather than a per-card choice, so it is declared once here and
 * `previewCount` exists only for a surface that has argued its way out of it.
 */
export const PREVIEW_COUNT = 5

interface PreviewListProps<T> {
  /**
   * Every item the caller holds, already sorted, most relevant first. The
   * empty branch renders when this is empty, so a caller that pages must not
   * pass a single page: this primitive previews a list it can see all of.
   */
  items: T[]
  /**
   * The list's true total. Defaults to `items.length`, which is right only
   * because the callers hold the whole list; a caller reading a `count` from
   * the API passes that, since "Show all (N)" promises the real number.
   */
  total?: number
  /** Rows shown before "Show all". Defaults to the house cap. */
  previewCount?: number
  getKey: (item: T) => string
  /** The row component stays with its feature; this primitive owns no row. */
  renderItem: (item: T) => ReactNode
  isLoading?: boolean
  isError?: boolean
  error?: unknown
  onRetry?: () => void
  /** What failed, in the caller's words ("Couldn't load app sessions"). */
  errorFallback: string
  /** The caller's one-sentence empty state. */
  empty: ReactNode
  skeletonRows?: number
  skeletonClassName?: string
  /** Opens the full list. The link renders only when `total > previewCount`. */
  onShowAll?: () => void
}

/**
 * The P5 preview list: the first few rows of a list managed elsewhere, and a
 * "Show all (N)" way to the rest.
 *
 * Extracted from the two real shapes that agreed — `Agents/AgentHandovers.tsx`
 * and `UserSettings/DesktopSessionsCard.tsx` — rather than designed ahead of a
 * second consumer (guidelines §5). What it makes structural instead of
 * per-author is the pair of checks a hand-written card keeps failing: the cap
 * (R4), and four *distinct* states (R10) — in particular an error that renders
 * as an error, since `data ?? []` in the caller turns a failed request into
 * "nothing here yet".
 *
 * The branch order is fixed and is the point of the component:
 * `isError` → `isLoading` → empty → rows.
 *
 * Not included: the "Show all" destination. A Sheet whose title, description,
 * empty copy and rows are all caller-supplied would save a handful of lines and
 * cost an indirection, so each consumer keeps its own.
 */
export function PreviewList<T>({
  items,
  total,
  previewCount = PREVIEW_COUNT,
  getKey,
  renderItem,
  isLoading,
  isError,
  error,
  onRetry,
  errorFallback,
  empty,
  skeletonRows = 3,
  skeletonClassName = "h-[52px] w-full rounded-lg",
  onShowAll,
}: PreviewListProps<T>) {
  if (isError) {
    return (
      <QueryErrorAlert
        error={error}
        fallback={errorFallback}
        onRetry={onRetry}
      />
    )
  }

  if (isLoading) {
    return (
      <div className="space-y-1.5">
        {Array.from({ length: skeletonRows }, (_, i) => (
          <Skeleton key={i} className={skeletonClassName} />
        ))}
      </div>
    )
  }

  if (items.length === 0) return <>{empty}</>

  const totalCount = total ?? items.length

  return (
    <div className="space-y-1.5">
      {/* A Fragment, not a wrapper div: `space-y-1.5` must reach the caller's
          own row element, exactly as it did before the extraction. */}
      {items.slice(0, previewCount).map((item) => (
        <Fragment key={getKey(item)}>{renderItem(item)}</Fragment>
      ))}
      {onShowAll && totalCount > previewCount && (
        <Button variant="link" size="sm" className="px-0" onClick={onShowAll}>
          Show all ({totalCount})
        </Button>
      )}
    </div>
  )
}
