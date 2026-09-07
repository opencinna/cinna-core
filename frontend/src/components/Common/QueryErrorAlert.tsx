import { AlertCircle } from "lucide-react"
import type { ReactNode } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { getErrorMessage } from "@/utils"

/**
 * A failed read, rendered as a failure — never as an empty state.
 *
 * The guidelines' fourth state (§6 "Error"): an `Alert variant="destructive"`
 * in place of the content, carrying the server's own words and a Retry. It
 * exists as a shared component because the alternative is what this codebase
 * already had — a one-line `text-destructive` here, a dashed "nothing yet"
 * panel there — and the second shape is the dangerous one: an admin who reads
 * "no managed AI credentials yet" after a 500 concludes the list is empty and
 * goes and creates a duplicate.
 *
 * Extracted from `Admin/ServerChannels/RoutingStateBlocks.tsx`'s `RoutingError`,
 * which now renders through it and contributes only its own extra sentence via
 * `children`. Four callers on the Access tab plus that one is well past the
 * point where a local copy is cheaper than a component.
 *
 * `onRetry` is optional but should be passed whenever the caller holds a
 * `refetch`: an error panel with no way out is a dead end on a page whose only
 * other recovery is a full reload.
 */
export function QueryErrorAlert({
  error,
  fallback,
  onRetry,
  compact,
  children,
}: {
  error: unknown
  /** What failed, in the caller's words. Shown when the API gave no detail. */
  fallback: string
  onRetry?: () => void
  compact?: boolean
  /** Caller-specific clarification, rendered under the server's message. */
  children?: ReactNode
}) {
  return (
    <Alert variant="destructive" className={compact ? "px-3 py-2" : undefined}>
      <AlertCircle />
      <AlertTitle>{fallback}</AlertTitle>
      <AlertDescription>
        <p className="text-xs break-words">
          {getErrorMessage(error, "The request failed.")}
        </p>
        {children}
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
      </AlertDescription>
    </Alert>
  )
}
