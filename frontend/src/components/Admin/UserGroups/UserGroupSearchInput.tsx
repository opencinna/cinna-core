import { Search, X } from "lucide-react"
import { forwardRef, useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/** `value`, settled for `delayMs` — what a server search should query. */
export function useDebouncedValue<T>(value: T, delayMs = 250): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(handle)
  }, [value, delayMs])
  return debounced
}

interface UserGroupSearchInputProps {
  value: string
  onChange: (value: string) => void
  /** Called by the clear `X`; the caller moves focus back to the input. */
  onClear: () => void
  ariaLabel: string
}

/**
 * The toolbar search of the User Groups list and the group's two tabs, built
 * from the `credentials.tsx` skeleton: a `Search` glyph, and a tooltipped clear
 * `X` once something is typed. Debouncing is the caller's
 * (`useDebouncedValue`): the Shares tab filters in the browser and needs none.
 */
export const UserGroupSearchInput = forwardRef<
  HTMLInputElement,
  UserGroupSearchInputProps
>(function UserGroupSearchInput({ value, onChange, onClear, ariaLabel }, ref) {
  return (
    <div className="relative min-w-40 max-w-64 flex-1">
      <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        ref={ref}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Search"
        aria-label={ariaLabel}
        className="pl-9 pr-9"
      />
      {value && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="absolute top-1/2 right-1 h-7 w-7 -translate-y-1/2"
              onClick={onClear}
              aria-label="Clear search"
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Clear search
          </TooltipContent>
        </Tooltip>
      )}
    </div>
  )
})

/** The centred empty / no-match block inside a table (§6 Empty, on routes). */
export function TableEmptyBlock({
  text,
  children,
}: {
  text: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-3 py-6 whitespace-normal">
      <p className="max-w-md text-sm text-muted-foreground">{text}</p>
      {children}
    </div>
  )
}
