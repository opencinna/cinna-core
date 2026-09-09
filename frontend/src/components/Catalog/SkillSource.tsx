import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import { splitSkillFrontmatter } from "@/utils/skills"

interface SkillSourceProps {
  content: string | undefined
  truncated?: boolean
  isLoading?: boolean
  isError?: boolean
  error?: unknown
  onRetry?: () => void
  /** What failed, in the caller's words. */
  errorFallback: string
  /** Rendered above the source — the shadowing note, a revision label. */
  notice?: React.ReactNode
  /**
   * Overrides the pane's height cap. The 60vh default is right for a host that
   * shows nothing else; a dialog that stacks a skill list above this one needs
   * a smaller share of the viewport, and the host is the only thing that knows
   * how much room it has left.
   */
  className?: string
}

/**
 * A `SKILL.md`, rendered.
 *
 * The body is **rendered markdown** — a skill's instructions are prose with
 * headings, lists and fenced examples, and a `<pre>` of that is a wall the
 * reader has to parse by eye (it started raw; the switch was asked for after
 * a many-skill plugin made the raw pane unreadable). The frontmatter is split
 * off first: its two meaningful keys are already the dialog's title and
 * description, and rendered as markdown it is a stray rule over `key: value`
 * lines.
 *
 * Extracted at the second consumer, not before (§5): S2's dialog
 * (`Agents/SkillContentBody`) had this block first, and the package route's
 * "SKILL.md" card is the second — two real shapes that agreed, so the scroll
 * cap, the four states and the truncation note stop being two copies that can
 * drift. The frame around it stays with each caller: one is a dialog body, the
 * other a card.
 */
export function SkillSource({
  content,
  truncated,
  isLoading,
  isError,
  error,
  onRetry,
  errorFallback,
  notice,
  className,
}: SkillSourceProps) {
  return (
    <div
      className={cn(
        "max-h-[60vh] min-w-0 overflow-x-auto overflow-y-auto rounded-md border bg-muted/30 p-3",
        className,
      )}
    >
      {isError ? (
        // A failed read renders as a failure — never as an empty file.
        <QueryErrorAlert
          error={error}
          fallback={errorFallback}
          onRetry={onRetry}
        />
      ) : isLoading ? (
        <Skeleton className="h-[240px] w-full" />
      ) : (
        <>
          {notice}
          <MarkdownRenderer
            content={splitSkillFrontmatter(content ?? "").body}
            className="prose prose-sm dark:prose-invert max-w-none break-words"
          />
          {truncated && (
            <p className="mt-2 text-xs text-muted-foreground">
              This file is longer than the viewer shows; the rest is on disk.
            </p>
          )}
        </>
      )}
    </div>
  )
}
