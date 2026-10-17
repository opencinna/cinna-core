import { type KeyboardEvent, useRef } from "react"

import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

export interface Segment<T extends string> {
  value: T
  label: string
  /** A count after the label; `null` while it loads, absent for none. */
  count?: number | null
}

interface ArtifactSegmentsProps<T extends string> {
  segments: Segment<T>[]
  value: T
  onChange: (next: T) => void
  /** The radiogroup's accessible name ("Artifact scope"). */
  label: string
  className?: string
}

/**
 * The segmented control of §2 Page toolbar on the `CredentialSectionTabs`
 * skeleton — one bordered track, the `bg-primary` on-state, `radiogroup` /
 * `radio` + `aria-checked` with a roving tab stop and arrow keys. The
 * artifacts list's scopes and the share dialog's two halves both use it.
 */
export function ArtifactSegments<T extends string>({
  segments,
  value,
  onChange,
  label,
  className,
}: ArtifactSegmentsProps<T>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const step =
      e.key === "ArrowRight" || e.key === "ArrowDown"
        ? 1
        : e.key === "ArrowLeft" || e.key === "ArrowUp"
          ? -1
          : 0
    if (step === 0) return
    e.preventDefault()
    const next = (index + step + segments.length) % segments.length
    onChange(segments[next].value)
    refs.current[next]?.focus()
  }

  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn(
        "inline-flex flex-wrap items-center gap-1 rounded-lg border bg-muted/50 p-1",
        className,
      )}
    >
      {segments.map((segment, index) => {
        const checked = segment.value === value
        return (
          // biome-ignore lint/a11y/useSemanticElements: a segmented control on the shared track; native radios cannot carry this skin, and the roving tab stop is implemented here.
          <button
            key={segment.value}
            ref={(el) => {
              refs.current[index] = el
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(segment.value)}
            onKeyDown={(e) => onKeyDown(e, index)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium whitespace-nowrap",
              "transition-colors outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
              checked
                ? "bg-primary text-primary-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {segment.label}
            {segment.count === null ? (
              <Skeleton className="h-4 w-5" />
            ) : segment.count !== undefined ? (
              <span
                className={cn(
                  "tabular-nums",
                  checked ? "opacity-80" : "text-muted-foreground",
                )}
              >
                {segment.count}
              </span>
            ) : null}
          </button>
        )
      })}
    </div>
  )
}
