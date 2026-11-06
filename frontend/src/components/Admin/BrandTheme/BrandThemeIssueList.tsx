import { AlertCircle, TriangleAlert } from "lucide-react"

import type { BrandThemeIssue } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { groupIssues } from "./brandThemeIssues"

interface BrandThemeIssueListProps {
  issues: BrandThemeIssue[]
  /** `error`: the package or draft was refused. `warning`: notes, non-blocking. */
  tone: "error" | "warning"
  /** The caller's one-sentence summary of what happened. */
  headline: string
}

/**
 * Validator findings as one alert, grouped General · Dark · Light (S5). Each
 * item is the dotted path and the server's message, which says what to
 * change; the stable `code` is for tools and is not shown.
 *
 * No scroll cap of its own: it lives in a dialog body that already scrolls,
 * and a nested scroller would fight it.
 */
export function BrandThemeIssueList({
  issues,
  tone,
  headline,
}: BrandThemeIssueListProps) {
  const groups = groupIssues(issues)
  const isError = tone === "error"
  return (
    <Alert
      variant={isError ? "destructive" : "default"}
      className={isError ? undefined : "[&>svg]:text-warning"}
    >
      {isError ? <AlertCircle /> : <TriangleAlert />}
      <AlertTitle className="line-clamp-none break-words">
        {headline}
      </AlertTitle>
      <AlertDescription className="mt-1 gap-3">
        {groups.map((group) => (
          <section key={group.key} className="w-full min-w-0 space-y-1.5">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {group.label} · {group.issues.length}
            </h4>
            <ul className="space-y-2">
              {group.issues.map((issue, index) => (
                <li key={`${issue.path}-${issue.code}-${index}`}>
                  <p className="font-mono text-xs break-all">{issue.path}</p>
                  <p className="text-sm break-words">{issue.message}</p>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </AlertDescription>
    </Alert>
  )
}
