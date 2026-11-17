import { AlertTriangle, ShieldCheck } from "lucide-react"
import type { ReactNode } from "react"

import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { pluralize } from "@/utils/pluralize"

/** Scope chips shown before "+N more". */
const SCOPES_SHOWN = 5

interface AgentApiConnectionScopesCardProps {
  scopes: string[]
  /** One sentence under the title: who sets these, and whether it's here. */
  description: string
  /** What these scopes reach, under the chips. */
  reach: string
  /** The answer when there are none. */
  emptyText: string
  /** Shows "Edit scopes" on the title row. */
  onEdit?: () => void
  /** Scopes the API no longer declares (owner only, once the catalog is in). */
  undeclaredCount?: number
  isLoading?: boolean
  error?: unknown
  onRetry?: () => void
}

/**
 * "Connection scopes" — the scopes an agent API connection adds for every
 * caller. The owner's credential page passes `onEdit`; a recipient's view
 * shows the same card read-only, so both read the connection the same way.
 */
export function AgentApiConnectionScopesCard({
  scopes,
  description,
  reach,
  emptyText,
  onEdit,
  undeclaredCount = 0,
  isLoading,
  error,
  onRetry,
}: AgentApiConnectionScopesCardProps) {
  const shown = scopes.slice(0, SCOPES_SHOWN)
  const rest = scopes.slice(SCOPES_SHOWN)

  let body: ReactNode
  if (isLoading) {
    body = (
      <div className="space-y-2">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-5 w-full" />
      </div>
    )
  } else if (error) {
    body = (
      <QueryErrorAlert
        error={error}
        fallback="Couldn't load this connection's scopes"
        onRetry={onRetry}
        compact
      />
    )
  } else if (scopes.length === 0) {
    body = <p className="text-sm text-muted-foreground">{emptyText}</p>
  } else {
    body = (
      <div className="space-y-3">
        <div className="space-y-1.5">
          <p className="text-sm">
            {pluralize(scopes.length, "scope", "scopes")}
          </p>
          <div className="flex flex-wrap items-center gap-1">
            {shown.map((scope) => (
              <Badge
                key={scope}
                variant="secondary"
                className="h-5 font-mono text-xs"
              >
                {scope}
              </Badge>
            ))}
            {rest.length > 0 && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    className="text-xs text-muted-foreground hover:underline"
                  >
                    +{rest.length} more
                  </button>
                </TooltipTrigger>
                <TooltipContent className="max-w-xs break-words font-mono">
                  {rest.join(", ")}
                </TooltipContent>
              </Tooltip>
            )}
          </div>
        </div>
        <p className="text-xs text-muted-foreground break-words">{reach}</p>
        {undeclaredCount > 0 && (
          <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
            <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" />
            <span>
              {undeclaredCount === 1
                ? "1 scope is no longer declared by the API."
                : `${undeclaredCount} scopes are no longer declared by the API.`}
            </span>
          </p>
        )}
      </div>
    )
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <ShieldCheck className="h-5 w-5 shrink-0" />
            Connection scopes
          </CardTitle>
          {onEdit && (
            <Button
              size="sm"
              variant="outline"
              className="shrink-0"
              onClick={onEdit}
            >
              Edit scopes
            </Button>
          )}
        </div>
        <CardDescription className="break-words">{description}</CardDescription>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  )
}
