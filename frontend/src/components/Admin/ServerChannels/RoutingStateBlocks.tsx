import { AlertCircle, Info, Timer } from "lucide-react"
import type { ReactNode } from "react"
import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { getErrorMessage } from "@/utils"
import { isRateLimited, retryAfterSeconds } from "./routingRateLimit"

/**
 * The four states, made visually and textually distinct — once, so every panel
 * on this card renders them the same way.
 *
 * This card's whole job is telling an admin why routing did what it did. A
 * failed request that renders as "no routing decisions yet" would answer that
 * question **wrongly**: the admin concludes routing never ran. So error and
 * empty do not share a shape here — error is destructive-toned with an icon and
 * a retry, empty is a dashed muted panel, and neither can be mistaken for the
 * other at a glance.
 *
 * `RoutingNotice` is the third, easily-missed case: a server-authored sentence
 * explaining why a panel is empty *on purpose* (tracing switched off, message
 * text gated, nothing to rank). It is rendered verbatim and never merged into
 * the empty copy.
 */

export function RoutingLoading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, i) => (
        // Static placeholder list — index keys are the only key available and
        // the list never reorders.
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  )
}

export function RoutingError({
  error,
  fallback,
  onRetry,
  compact,
}: {
  error: unknown
  /** What failed, in the caller's words. Shown when the API gave no detail. */
  fallback: string
  onRetry?: () => void
  compact?: boolean
}) {
  return (
    <div
      role="alert"
      className={`flex items-start gap-2 rounded-lg border border-destructive/50 bg-destructive/5 ${
        compact ? "px-3 py-2" : "p-4"
      }`}
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-medium text-destructive">{fallback}</p>
        <p className="text-xs break-words text-muted-foreground">
          {getErrorMessage(error, "The request failed.")}
        </p>
        {/* Named explicitly so nobody reads a failed panel as a finding about
            routing. This is the whole reason the error branch exists. */}
        <p className="text-xs text-muted-foreground">
          This is a failure to load — not a statement about what routing did.
        </p>
        {onRetry && (
          <Button
            variant="outline"
            size="sm"
            className="mt-1 h-7 text-xs"
            onClick={onRetry}
          >
            Try again
          </Button>
        )}
      </div>
    </div>
  )
}

export function RoutingEmpty({
  title,
  hint,
  icon,
}: {
  title: string
  hint?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div className="rounded-lg border border-dashed p-6 text-center">
      {icon && <div className="mb-2 flex justify-center opacity-50">{icon}</div>}
      <p className="text-sm text-muted-foreground">{title}</p>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

/**
 * A server-authored explanation, rendered verbatim.
 *
 * Callers pass `notice` straight from the API (`RoutingDecisionsPublic.notice`,
 * `message_text_notice`, `near_miss_notice`, the recommendation's advisory
 * `notice`). Those sentences are computed and tested on the backend precisely
 * so the UI cannot overstate what a gate did — nothing here rewrites them.
 */
export function RoutingNotice({
  notice,
  className = "",
}: {
  notice: string
  className?: string
}) {
  return (
    <div
      className={`flex items-start gap-2 rounded-lg border border-amber-500/50 bg-amber-500/5 px-3 py-2 ${className}`}
    >
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
      <p className="min-w-0 flex-1 text-xs break-words">{notice}</p>
    </div>
  )
}

/**
 * A mutation failure, with the throttle told apart from everything else.
 *
 * simulate / replay / recommendation share one per-admin bucket and answer 429
 * with `Retry-After`. Rendering that as a generic red box would tell an admin
 * their tuning tool is broken when in fact they are simply early, so the 429
 * gets its own wording and a live countdown.
 *
 * The countdown is only shown when the header was actually captured
 * (`retryAfterSeconds()` returns `null` otherwise). An invented number would be
 * the same failure in a smaller box.
 */
export function RoutingMutationError({
  error,
  fallback,
  onRetry,
}: {
  error: unknown
  fallback: string
  onRetry?: () => void
}) {
  if (isRateLimited(error)) {
    return <RateLimited error={error} />
  }
  return <RoutingError error={error} fallback={fallback} onRetry={onRetry} compact />
}

function RateLimited({ error }: { error: unknown }) {
  const [seconds, setSeconds] = useState<number | null>(() => retryAfterSeconds())
  // Armed only while there is a number still counting down. Keying on
  // `seconds !== null` alone would leave a 1s interval ticking forever once it
  // reached zero, for as long as the error stayed mounted.
  const isTicking = seconds !== null && seconds > 0

  useEffect(() => {
    if (!isTicking) return
    const handle = setInterval(() => setSeconds(retryAfterSeconds()), 1000)
    return () => clearInterval(handle)
  }, [isTicking])

  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-lg border border-amber-500/50 bg-amber-500/5 px-3 py-2"
    >
      <Timer className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-medium">
          {seconds === null || seconds <= 0
            ? "You're going too fast — try again in a moment."
            : `You're going too fast — try again in ${seconds}s.`}
        </p>
        {/* The backend names the limit and the setting that raises it. */}
        <p className="text-xs break-words text-muted-foreground">
          {getErrorMessage(error, "Rate limited.")}
        </p>
      </div>
    </div>
  )
}
