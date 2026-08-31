/**
 * GitDiffDialog — console-style unified diff for one changed item.
 *
 * The drill-down behind every row of the commit preview and the pull-conflict
 * dialog: click a prompt, setting or workspace file and see what actually
 * changed, rendered as `git diff` output (last synced revision on `a/`, live
 * agent on `b/`) rather than just "modified".
 *
 * Deliberately a plain `<pre>` with per-line coloring rather than a side-by-side
 * component: it is the format anyone who uses git already reads fluently, and it
 * survives long lines / large hunks with one horizontal scroller.
 */
import { useQuery } from "@tanstack/react-query"
import { AlertTriangle, FileWarning, Loader2 } from "lucide-react"

import { AgentGitService } from "@/client"
import { getErrorMessage } from "@/utils"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

/** What the caller wants diffed — the raw identifiers, never the label. */
export interface GitDiffTarget {
  /** "prompt" | "metadata" | "sdk" | "specs" | "file" */
  section: string
  /** Raw attribute name, or workspace-relative path for a file. */
  key: string
  /** Human label, shown while the request is in flight. */
  label: string
}

/**
 * Color one unified-diff line the way a terminal would. Order matters: the
 * `---` / `+++` file headers start with `-` / `+` and must be matched BEFORE
 * the add/remove cases, or they render as a deletion and an insertion.
 */
function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) {
    return "text-muted-foreground"
  }
  if (line.startsWith("@@")) return "text-sky-600 dark:text-sky-400"
  if (line.startsWith("+")) return "text-green-600 dark:text-green-400"
  if (line.startsWith("-")) return "text-red-600 dark:text-red-400"
  return "text-foreground/80"
}

interface GitDiffDialogProps {
  agentId: string
  /** `null` closes the dialog; a target opens (and re-fetches for) it. */
  target: GitDiffTarget | null
  onClose: () => void
}

export function GitDiffDialog({
  agentId,
  target,
  onClose,
}: GitDiffDialogProps) {
  const {
    data: diff,
    isFetching,
    isError,
    error,
  } = useQuery({
    queryKey: ["git-diff", agentId, target?.section, target?.key],
    queryFn: () =>
      AgentGitService.getGitDiff({
        agentId,
        section: target!.section,
        key: target!.key,
      }),
    enabled: !!target,
    // Same reasoning as the pull-conflict dialog's status read: a diff is only
    // useful if it reflects the agent as it is right now, and this is opened by
    // an explicit click, so never serve a cached body.
    staleTime: 0,
    retry: false,
  })

  // Only trust a body that belongs to the currently-open target — React Query
  // serves the previous key's data during a key switch, which would otherwise
  // show the last file's diff under this file's title for a frame.
  const isCurrent =
    !!target && diff?.section === target.section && diff?.key === target.key
  const lines = isCurrent && diff.diff ? diff.diff.split("\n") : []

  return (
    <Dialog open={!!target} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="font-mono text-sm break-all">
            {isCurrent ? diff.label : target?.label}
          </DialogTitle>
          <DialogDescription>
            <span className="font-mono">a/</span> last synced revision ·{" "}
            <span className="font-mono">b/</span> this agent now
            {isCurrent && diff.truncated ? " · truncated" : null}
          </DialogDescription>
        </DialogHeader>

        {isFetching || !isCurrent ? (
          <div className="flex items-center gap-2 py-6 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Loading diff…
          </div>
        ) : isError ? (
          <p className="flex items-start gap-1.5 py-4 text-xs text-destructive">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>{getErrorMessage(error, "Could not load this diff.")}</span>
          </p>
        ) : diff.binary ? (
          <p className="flex items-start gap-1.5 py-4 text-xs text-muted-foreground">
            <FileWarning className="h-3.5 w-3.5 shrink-0" />
            <span>Binary file — no text diff to show.</span>
          </p>
        ) : lines.length === 0 ? (
          <p className="py-4 text-xs text-muted-foreground">
            No differences against the last synced revision.
          </p>
        ) : (
          <pre className="max-h-[60vh] overflow-auto rounded-md border bg-muted/40 p-3 text-[11px] leading-relaxed">
            {lines.map((line, i) => (
              // Index keys are correct here: the list is static text that is
              // replaced wholesale, never reordered or edited in place.
              <div key={i} className={`font-mono ${diffLineClass(line)}`}>
                {line === "" ? " " : line}
              </div>
            ))}
          </pre>
        )}

        {isCurrent && diff.truncated && !diff.binary && (
          <p className="text-[11px] text-muted-foreground">
            This diff was truncated. Use the remote host or your local clone to
            review it in full.
          </p>
        )}
      </DialogContent>
    </Dialog>
  )
}
