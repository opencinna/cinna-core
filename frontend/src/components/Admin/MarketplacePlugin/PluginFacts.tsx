import type { ReactNode } from "react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"

/** One labelled fact in a detail card's fact grid. */
export function Fact({
  label,
  children,
  className,
}: {
  label: string
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className="text-sm font-medium text-muted-foreground">{label}</div>
      <div className="mt-1 text-sm">{children}</div>
    </div>
  )
}

/**
 * A visible value that exists to be pasted (a digest, a commit hash) as its
 * own copy control (§2 Copying a visible value): `display` is what is shown,
 * `value` what is copied — a short hash on screen, the full one on the
 * clipboard.
 */
export function CopyValueButton({
  value,
  display,
  noun,
}: {
  value: string
  display: string
  /** "Digest" → "Digest copied" / "Copy the digest". */
  noun: string
}) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      showSuccessToast(`${noun} copied`)
    } catch {
      // `navigator.clipboard` is undefined outside a secure context.
      showErrorToast(`Failed to copy the ${noun.toLowerCase()}`)
    }
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="max-w-full cursor-pointer truncate rounded font-mono text-sm hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          aria-label={`Copy the ${noun.toLowerCase()}`}
          onClick={() => void copy()}
        >
          {display}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Click to copy
      </TooltipContent>
    </Tooltip>
  )
}
