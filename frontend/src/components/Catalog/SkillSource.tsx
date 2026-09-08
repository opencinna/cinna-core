import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Skeleton } from "@/components/ui/skeleton"

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
}

/**
 * A `SKILL.md`, exactly as the model receives it.
 *
 * The **raw** file, frontmatter included, in a `<pre>`: rendering the markdown
 * would hide the frontmatter, reflow fenced blocks and add a viewer neither
 * consumer needs, and the stated intent on both surfaces is to show what the
 * engine is handed rather than a prettier version of it.
 *
 * Extracted at the second consumer, not before (§5): S2's dialog
 * (`Agents/SkillContentDialog`) had this block first, and the package route's
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
}: SkillSourceProps) {
  return (
    <div className="max-h-[60vh] overflow-y-auto rounded-md border bg-muted/30 p-3">
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
          <pre className="font-mono text-xs whitespace-pre-wrap break-words">
            {content ?? ""}
          </pre>
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
