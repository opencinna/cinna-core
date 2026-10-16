import { type KeyboardEvent, useRef } from "react"

import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

/**
 * The server's ``category`` values (`CredentialsService.classify_credential_
 * category`): which section a credential is listed under. The page never
 * re-derives it.
 */
export type CredentialSection = "mine" | "automatic" | "bundle"

const SEGMENTS: { value: CredentialSection; label: string }[] = [
  { value: "mine", label: "My Credentials" },
  { value: "automatic", label: "Automatic Credentials" },
  { value: "bundle", label: "Bundle Credentials" },
]

interface CredentialSectionTabsProps {
  value: CredentialSection
  onChange: (next: CredentialSection) => void
  /** Rows per section; `undefined` while the lists are loading. */
  counts: Record<CredentialSection, number> | undefined
}

/**
 * Where on the Credentials page you are — the segmented control of §2 Page
 * toolbar, on the `Catalog/CatalogKindTabs` skeleton: one bordered track, the
 * `bg-primary` on-state, `radiogroup`/`radio` + `aria-checked` with a roving
 * tab stop and arrow keys.
 */
export function CredentialSectionTabs({
  value,
  onChange,
  counts,
}: CredentialSectionTabsProps) {
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
    const next = (index + step + SEGMENTS.length) % SEGMENTS.length
    onChange(SEGMENTS[next].value)
    refs.current[next]?.focus()
  }

  return (
    <div
      role="radiogroup"
      aria-label="Credential section"
      className="inline-flex flex-wrap items-center gap-1 rounded-lg border bg-muted/50 p-1"
    >
      {SEGMENTS.map((segment, index) => {
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
              // The on-state is the brand colour: `bg-background` is darker than
              // the track on the dark skin, and `bg-accent` is the hover colour.
              checked
                ? "bg-primary text-primary-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {segment.label}
            {counts ? (
              <span
                className={cn(
                  "tabular-nums",
                  checked ? "opacity-80" : "text-muted-foreground",
                )}
              >
                {counts[segment.value]}
              </span>
            ) : (
              <Skeleton className="h-4 w-5" />
            )}
          </button>
        )
      })}
    </div>
  )
}
