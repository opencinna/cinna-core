import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import {
  accessModeCopy,
  MARKETPLACE_ACCESS_MODE_VALUES,
  type MarketplaceAccessMode,
} from "@/utils/marketplace"

interface MarketplaceAccessModeFieldProps {
  /** `undefined` until the admin chooses: the create field has no default. */
  value: MarketplaceAccessMode | undefined
  onChange: (mode: MarketplaceAccessMode) => void
  /** The stored mode, marked "Current" on its tile (S11 only). */
  currentMode?: MarketplaceAccessMode
  /** The id of the visible label naming the group. */
  labelledBy: string
  /** The id of an error line under the group, when one is shown. */
  describedBy?: string
  disabled?: boolean
}

/**
 * Who sees this marketplace's entries — two radio tiles with one help
 * sentence each. Shared by Add marketplace (S9) and the Change plugin access
 * dialog (S11), so the choice reads identically before and after.
 */
export function MarketplaceAccessModeField({
  value,
  onChange,
  currentMode,
  labelledBy,
  describedBy,
  disabled,
}: MarketplaceAccessModeFieldProps) {
  return (
    <div
      role="radiogroup"
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      aria-required
      className="grid gap-2"
    >
      {MARKETPLACE_ACCESS_MODE_VALUES.map((mode) => {
        const selected = value === mode
        const copy = accessModeCopy(mode)
        return (
          // biome-ignore lint/a11y/useSemanticElements: a tile with a title and a help line; a native radio cannot hold both.
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => onChange(mode)}
            className={cn(
              "rounded-md border px-3 py-2.5 text-left transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:pointer-events-none disabled:opacity-60",
              selected && "border-primary bg-primary/5",
            )}
          >
            <span className="flex items-center gap-2 text-sm font-medium">
              {copy.label}
              {currentMode === mode && (
                <Badge variant="secondary" className="h-5">
                  Current
                </Badge>
              )}
            </span>
            <span className="block text-xs text-muted-foreground">
              {copy.help}
            </span>
          </button>
        )
      })}
    </div>
  )
}
