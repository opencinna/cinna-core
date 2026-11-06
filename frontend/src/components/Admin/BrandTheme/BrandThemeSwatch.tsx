import { Palette } from "lucide-react"

import type { BrandThemePreviewLook } from "@/components/BrandTheme/brandTheme"
import { cn } from "@/lib/utils"

interface BrandThemeSwatchProps {
  /** The parsed draft, or null when its document fails the parser. */
  look: BrandThemePreviewLook | null
  className?: string
}

/**
 * A theme's identity tile: its light-mode accent beside its dark-mode accent,
 * both read through the hex-checked parser. A draft that does not parse gets
 * the stock tile instead of a guess.
 */
export function BrandThemeSwatch({ look, className }: BrandThemeSwatchProps) {
  if (!look) {
    return (
      <span
        className={cn(
          "flex h-6 w-6 shrink-0 items-center justify-center rounded bg-muted",
          className,
        )}
      >
        <Palette className="h-3.5 w-3.5 text-muted-foreground" />
      </span>
    )
  }
  return (
    <span
      className={cn(
        "flex h-6 w-6 shrink-0 overflow-hidden rounded border",
        className,
      )}
    >
      <span
        className="flex-1"
        style={{ backgroundColor: look.modes.light.tokens["--primary"] }}
      />
      <span
        className="flex-1"
        style={{ backgroundColor: look.modes.dark.tokens["--primary"] }}
      />
    </span>
  )
}
