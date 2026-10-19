import type { ReactNode } from "react"

import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"

interface ApiAccessSwitchLineProps {
  id: string
  label: string
  hint: ReactNode
  checked: boolean
  disabled: boolean
  onChange: (checked: boolean) => void
}

/** A card-level auto-saving boolean: label and one-line hint, Switch right. */
export function ApiAccessSwitchLine({
  id,
  label,
  hint,
  checked,
  disabled,
  onChange,
}: ApiAccessSwitchLineProps) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0 space-y-0.5">
        <Label htmlFor={id} className="text-sm font-medium">
          {label}
        </Label>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </div>
      <Switch
        id={id}
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
        className="mt-0.5 shrink-0"
      />
    </div>
  )
}
