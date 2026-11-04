import type { DashboardComponentPublic } from "@/client"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  durationLabel,
  type RefreshDefaults,
  readRefreshDefaults,
} from "./componentSpec"
import type { RefreshOverrides } from "./tileConfig"

/** Radix `Select` values cannot be empty: this stands for "no override". */
export const INHERIT = "inherit"

const MAX_AGE = ["1m", "5m", "15m", "1h", "6h", "1d"]
// The backend refuses anything under a minute (`DURATION_1M_PATTERN`).
const WHILE_OPEN = ["1m", "5m", "15m"]

export interface RefreshDraft {
  max_age: string
  while_open_every: string
  after_turn: string
}

export function draftFromOverrides(refresh: RefreshOverrides): RefreshDraft {
  return {
    max_age: refresh.max_age || INHERIT,
    while_open_every: refresh.while_open_every || INHERIT,
    after_turn:
      refresh.after_turn === true
        ? "on"
        : refresh.after_turn === false
          ? "off"
          : INHERIT,
  }
}

/** Only the overridden keys are stored; an inherited one is left out. */
export function overridesFromDraft(draft: RefreshDraft): RefreshOverrides {
  const out: RefreshOverrides = {}
  if (draft.max_age !== INHERIT) out.max_age = draft.max_age
  if (draft.while_open_every !== INHERIT)
    out.while_open_every = draft.while_open_every
  if (draft.after_turn !== INHERIT) out.after_turn = draft.after_turn === "on"
  return out
}

function withCurrent(options: string[], current: string) {
  return current === INHERIT ||
    current === "manual" ||
    options.includes(current)
    ? options
    : [...options, current]
}

function inheritLabel(known: boolean, value: string) {
  return known ? `Component default (${value})` : "Component default"
}

interface Props {
  draft: RefreshDraft
  onChange: (draft: RefreshDraft) => void
  /** The component, for "Component default (<value>)"; absent when unreadable. */
  component: DashboardComponentPublic | undefined
}

/**
 * The Edit tile dialog's Refresh section (UI spec S3): three `Select`s, each
 * with "Component default (<value>)" as its inherit option, read from the
 * component's own `refresh` (UQ1). There is no `on_app_start` control: the
 * cloud has no app launch (D-C6).
 */
export function RefreshOverridesSection({ draft, onChange, component }: Props) {
  const known = !!component
  const defaults: RefreshDefaults = readRefreshDefaults(component)
  const set = (patch: Partial<RefreshDraft>) => onChange({ ...draft, ...patch })

  return (
    <section className="space-y-3">
      <h3 className="text-sm font-medium">Refresh</h3>

      <div className="space-y-1.5">
        <Label htmlFor="refresh-max-age">Keep results for</Label>
        <Select
          value={draft.max_age}
          onValueChange={(v) => set({ max_age: v })}
        >
          <SelectTrigger id="refresh-max-age" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={INHERIT}>
              {inheritLabel(known, durationLabel(defaults.max_age))}
            </SelectItem>
            {withCurrent(MAX_AGE, draft.max_age).map((v) => (
              <SelectItem key={v} value={v}>
                {durationLabel(v)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="refresh-while-open">While open, refresh every</Label>
        <Select
          value={draft.while_open_every}
          onValueChange={(v) => set({ while_open_every: v })}
        >
          <SelectTrigger id="refresh-while-open" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={INHERIT}>
              {inheritLabel(
                known,
                defaults.while_open_every
                  ? durationLabel(defaults.while_open_every)
                  : "Manual",
              )}
            </SelectItem>
            <SelectItem value="manual">Manual</SelectItem>
            {withCurrent(WHILE_OPEN, draft.while_open_every).map((v) => (
              <SelectItem key={v} value={v}>
                {durationLabel(v)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground">
          Only while the tile is on screen and the agent's environment is
          running.
        </p>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="refresh-after-turn">After the agent replies</Label>
        <Select
          value={draft.after_turn}
          onValueChange={(v) => set({ after_turn: v })}
        >
          <SelectTrigger id="refresh-after-turn" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={INHERIT}>
              {inheritLabel(known, defaults.after_turn ? "On" : "Off")}
            </SelectItem>
            <SelectItem value="on">Refresh</SelectItem>
            <SelectItem value="off">Don't refresh</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </section>
  )
}
