import { SlidersHorizontal } from "lucide-react"
import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Toggle } from "@/components/ui/toggle"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type { ParamSpec } from "./componentSpec"
import type { ParamsRefusal } from "./tileController"

const VISIBLE_CONTROLS = 4
/** Radix Select values cannot be empty: this item clears the param to its default. */
const DEFAULT_OPTION = "__default__"

interface Props {
  specs: ParamSpec[]
  stored: Record<string, unknown>
  resolved: Record<string, unknown>
  refusal: ParamsRefusal | null
  onChange: (name: string, value: unknown) => void
}

/**
 * The tile's params row (UI spec S1 §2): one control per spec, ≤ 4 visible and
 * the rest behind "More filters (N)". Auto-save: one change, one `PUT params`;
 * clearing a control returns it to its default. A refusal is the row's last
 * item and clears on the next accepted change.
 */
export function ParamsRow({
  specs,
  stored,
  resolved,
  refusal,
  onChange,
}: Props) {
  if (specs.length === 0) return null
  const visible = specs.slice(0, VISIBLE_CONTROLS)
  const more = specs.slice(VISIBLE_CONTROLS)
  const currentValue = (spec: ParamSpec) =>
    stored[spec.name] ?? resolved[spec.name]

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b px-2 py-1.5">
      {visible.map((spec) => (
        <ParamControl
          key={spec.name}
          spec={spec}
          value={currentValue(spec)}
          onChange={(value) => onChange(spec.name, value)}
        />
      ))}
      {more.length > 0 && (
        <Popover>
          <PopoverTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 px-2 text-xs"
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
              More filters ({more.length})
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-64 space-y-2 p-3">
            {more.map((spec) => (
              <ParamControl
                key={spec.name}
                spec={spec}
                value={currentValue(spec)}
                onChange={(value) => onChange(spec.name, value)}
                stacked
              />
            ))}
          </PopoverContent>
        </Popover>
      )}
      {refusal && <RefusalLine refusal={refusal} />}
    </div>
  )
}

function RefusalLine({ refusal }: { refusal: ParamsRefusal }) {
  const text = refusal.field
    ? `Value refused: ${refusal.field} — ${refusal.message}`
    : `Value refused: ${refusal.message}`
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className="min-w-0 max-w-full truncate text-xs text-destructive"
          role="alert"
        >
          {text}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-xs text-xs">
        {text}
      </TooltipContent>
    </Tooltip>
  )
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return ""
  return typeof value === "string" ? value : String(value)
}

function ParamControl({
  spec,
  value,
  onChange,
  stacked = false,
}: {
  spec: ParamSpec
  value: unknown
  onChange: (value: unknown) => void
  stacked?: boolean
}) {
  const width = stacked ? "w-full" : undefined

  if (spec.type === "enum") {
    return (
      <Select
        value={asText(value) || DEFAULT_OPTION}
        onValueChange={(v) => onChange(v === DEFAULT_OPTION ? null : v)}
      >
        <SelectTrigger
          size="sm"
          className={cn("gap-1 px-2 text-xs data-[size=sm]:h-7", width)}
          aria-label={spec.label}
        >
          <span className="text-muted-foreground">{spec.label}:</span>
          <SelectValue placeholder="Any" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={DEFAULT_OPTION} className="text-xs">
            Default
          </SelectItem>
          {spec.values.map((option) => (
            <SelectItem key={option} value={option} className="text-xs">
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  }

  if (spec.type === "boolean") {
    return (
      <Toggle
        size="sm"
        variant="outline"
        pressed={value === true}
        onPressedChange={(pressed) => onChange(pressed)}
        className={cn("h-7 px-2 text-xs", width)}
      >
        {spec.label}
      </Toggle>
    )
  }

  if (spec.type === "date" || spec.type === "month") {
    return (
      <div className={cn("flex items-center gap-1 text-xs", width)}>
        <span className="text-muted-foreground" aria-hidden="true">
          {spec.label}
        </span>
        <Input
          type={spec.type}
          aria-label={spec.label}
          value={asText(value)}
          onChange={(event) => onChange(event.target.value || null)}
          className="h-7 w-auto px-2 text-xs"
        />
      </div>
    )
  }

  return (
    <CommitInput
      label={spec.label}
      numeric={spec.type === "number"}
      value={asText(value)}
      onCommit={(text) =>
        onChange(
          text === ""
            ? null
            : spec.type === "number" && !Number.isNaN(Number(text))
              ? Number(text)
              : text,
        )
      }
      className={stacked ? "w-full" : "w-28"}
    />
  )
}

/** Text and number params: committed on Enter or blur, never per keystroke. */
function CommitInput({
  label,
  numeric,
  value,
  onCommit,
  className,
}: {
  label: string
  numeric: boolean
  value: string
  onCommit: (text: string) => void
  className: string
}) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])

  const commit = () => {
    const text = draft.trim()
    if (text !== value) onCommit(text)
  }

  return (
    <Input
      type={numeric ? "number" : "text"}
      value={draft}
      placeholder={label}
      aria-label={label}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") commit()
        if (event.key === "Escape") setDraft(value)
      }}
      className={cn("h-7 px-2 text-xs", className)}
    />
  )
}
