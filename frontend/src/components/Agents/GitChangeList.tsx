/**
 * Shared `git status --short` style change presentation.
 *
 * Used by both the commit preview (`GitVersioningCard`) and the pull-conflict
 * dialog (`GitPullConflictDialog`) so the two dialogs cannot drift apart
 * visually — they describe the same change list from opposite directions
 * (what a commit would capture vs. what a pull would overwrite).
 */
import type { ReactNode } from "react"

// change_type → single-letter tag + color, mirroring `git status --short`.
export const CHANGE_META: Record<string, { tag: string; className: string }> = {
  added: { tag: "A", className: "text-green-600 dark:text-green-400" },
  modified: { tag: "M", className: "text-amber-600 dark:text-amber-400" },
  deleted: { tag: "D", className: "text-red-600 dark:text-red-400" },
}

export function ChangeRow({
  label,
  changeType,
  onOpenDiff,
}: {
  label: string
  changeType: string
  /** When given, the label becomes a button opening this row's diff. */
  onOpenDiff?: () => void
}) {
  const meta = CHANGE_META[changeType] ?? {
    tag: "?",
    className: "text-muted-foreground",
  }
  return (
    <li className="flex items-start gap-2 font-mono text-xs">
      <span className={`w-3 shrink-0 font-semibold ${meta.className}`}>
        {meta.tag}
      </span>
      {onOpenDiff ? (
        // A real <button>, not a clickable <span>: these rows live inside
        // dialogs, so keyboard reachability is the only way to review a change
        // without a mouse.
        <button
          type="button"
          onClick={onOpenDiff}
          title="View diff"
          className="break-all text-left underline decoration-dotted underline-offset-2 hover:text-primary hover:decoration-solid"
        >
          {label}
        </button>
      ) : (
        <span className="break-all">{label}</span>
      )}
    </li>
  )
}

/** A titled group of `ChangeRow`s (Prompts / Agent settings / Workspace). */
export function ChangeGroup({
  title,
  children,
}: {
  title: string
  children: ReactNode
}) {
  return (
    <div className="space-y-1">
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      <ul className="space-y-0.5">{children}</ul>
    </div>
  )
}
