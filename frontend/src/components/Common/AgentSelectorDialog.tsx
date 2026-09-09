import { useQuery } from "@tanstack/react-query"
import { Bot, Check, ChevronDown, Search, X } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { AgentsService } from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"
import { getColorPreset } from "@/utils/colorPresets"

export interface AgentOption {
  id: string
  name: string
  colorPreset: string | null | undefined
}

interface AgentPickerBaseProps {
  onSelect: (agentId: string) => void
  selectedAgentId?: string | null
  /** Pass a workspace ID to scope agents to that workspace. Omit for global. */
  workspaceId?: string | null
  /** Agent IDs to exclude from the list */
  excludeAgentIds?: string[]
  /** Override the fetched agents with a custom list (skips API call) */
  agents?: AgentOption[]
  title?: string
  /** Whether selecting an already-selected agent deselects it */
  allowDeselect?: boolean
  /** Height cap on the scrolling cloud. A form host has less room than a dialog. */
  maxHeightClassName?: string
  /** Frozen while a write this picker feeds is in flight. */
  disabled?: boolean
}

/**
 * The agents this picker offers, from wherever they come — a caller-supplied
 * list or the account's own agents. One hook so the cloud and the collapsed
 * single-select field resolve an agent the same way; React Query dedupes the
 * two subscriptions to one request.
 */
function useAgentOptions({
  workspaceId,
  excludeAgentIds,
  agents: externalAgents,
  enabled,
}: Pick<AgentPickerBaseProps, "workspaceId" | "excludeAgentIds" | "agents"> & {
  enabled: boolean
}) {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: workspaceId ? ["agents", workspaceId] : ["allAgents"],
    queryFn: () =>
      AgentsService.readAgents({
        skip: 0,
        limit: 200,
        ...(workspaceId ? { userWorkspaceId: workspaceId } : {}),
      }),
    enabled: enabled && !externalAgents,
  })

  const agents: AgentOption[] = useMemo(() => {
    const source =
      externalAgents ??
      (data?.data ?? []).map((a) => ({
        id: a.id,
        name: a.name,
        colorPreset: a.ui_color_preset,
      }))

    if (!excludeAgentIds?.length) return source
    const excludeSet = new Set(excludeAgentIds)
    return source.filter((a) => !excludeSet.has(a.id))
  }, [externalAgents, data, excludeAgentIds])

  return {
    agents,
    // A caller that passed its own list owns its own loading and error states.
    isLoading: isLoading && !externalAgents,
    isError: isError && !externalAgents,
    refetch,
  }
}

interface AgentCloudProps extends AgentPickerBaseProps {
  /** `single` announces one-of-n (radiogroup); `multi` is a plain group. */
  mode: "single" | "multi"
  /**
   * Whether the cloud is being shown right now. Gates the fetch and resets the
   * search, so a dialog can pass its `open` and an inline host can pass `true`.
   */
  active: boolean
}

/**
 * The picking surface itself — a search box over the colour-preset agent
 * cloud. Not exported: hosts take either `AgentSelectorDialog` (the picker as
 * a dialog) or `AgentSelectorList` (the picker as a form field), and both are
 * this same cloud, so the two can never drift into two different ways of
 * choosing an agent.
 */
function AgentCloud({
  onSelect,
  selectedAgentId,
  workspaceId,
  excludeAgentIds,
  agents: externalAgents,
  mode,
  allowDeselect = false,
  active,
  maxHeightClassName = "max-h-[300px]",
  disabled = false,
}: AgentCloudProps) {
  const [search, setSearch] = useState("")

  // Reset search when the picker is shown
  useEffect(() => {
    if (active) setSearch("")
  }, [active])

  const {
    agents: allAgents,
    isLoading,
    isError,
    refetch,
  } = useAgentOptions({
    workspaceId,
    excludeAgentIds,
    agents: externalAgents,
    enabled: active,
  })

  const filtered = useMemo(() => {
    if (!search.trim()) return allAgents
    const q = search.toLowerCase()
    return allAgents.filter((a) => a.name.toLowerCase().includes(q))
  }, [allAgents, search])

  // One-of-n is a radiogroup; the multi cloud is a plain group.
  const groupProps =
    mode === "single"
      ? ({ role: "radiogroup", "aria-label": "Agents" } as const)
      : ({ role: "group", "aria-label": "Agents" } as const)

  const handleSelect = (agentId: string) => {
    if (allowDeselect && mode === "multi" && agentId === selectedAgentId) {
      onSelect("")
      return
    }
    onSelect(agentId)
  }

  return (
    <div className="space-y-3">
      {/* Search input */}
      <div className="relative">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search agents..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-9"
          disabled={disabled}
        />
      </div>

      {/* A failed read is a failure, never an empty state: without this the
          catch-all below printed "No agents available" at somebody whose
          request 500ed, and they would go and create an agent they already
          have. */}
      {isError ? (
        <QueryErrorAlert
          error={null}
          fallback="Couldn't load your agents"
          onRetry={() => refetch()}
          compact
        />
      ) : isLoading ? (
        <p className="text-sm text-muted-foreground">Loading agents…</p>
      ) : (
        /* Agent cloud. `pr-2` because it scrolls: without it the rightmost
           pill of every row sits under the scrollbar (§2 "Scrolling lists and
           the far edge"). One-of-n is a radiogroup, not a row of toggles. */
        <div
          // Hoisted rather than a ternary on `role`: both roles accept
          // `aria-label`, but a dynamic `role` is opaque to the a11y lint,
          // which then reads the label as unsupported.
          {...groupProps}
          className={cn(
            "flex flex-wrap gap-2 overflow-y-auto pr-2",
            maxHeightClassName,
          )}
        >
          {filtered.map((agent) => {
            const preset = getColorPreset(agent.colorPreset)
            const isSelected = selectedAgentId === agent.id
            return (
              <button
                key={agent.id}
                type="button"
                // `aria-checked` for one-of-n, `aria-pressed` for the multi
                // cloud. The distinction is the one `Common/TooltipToggleItem`
                // was written to enforce: a single-select control announced as
                // "pressed / not pressed" tells a screen-reader user it is a
                // toggle they can switch off, when it is one choice among many.
                // The visual state hangs off a class, not `data-state`, so this
                // is only the ARIA half of that lesson.
                {...(mode === "single"
                  ? { role: "radio", "aria-checked": isSelected }
                  : { "aria-pressed": isSelected })}
                disabled={disabled}
                className={cn(
                  "cursor-pointer px-4 py-2 text-sm rounded-md transition-all flex items-center gap-2",
                  preset.badgeBg,
                  preset.badgeText,
                  preset.badgeHover,
                  isSelected && preset.badgeOutline,
                  disabled && "pointer-events-none opacity-60",
                )}
                onClick={() => handleSelect(agent.id)}
              >
                <Bot className="h-4 w-4" />
                {agent.name}
                {isSelected && <Check className="h-4 w-4" />}
              </button>
            )
          })}
          {filtered.length === 0 && (
            <p className="text-sm text-muted-foreground py-2">
              {allAgents.length === 0
                ? "No agents available"
                : "No agents match your search"}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

interface AgentSelectorListProps extends AgentPickerBaseProps {
  /**
   * `multi` (default) — every agent stays on screen as a pill and the chosen
   * ones carry an outline; the caller keeps whatever selection model it likes
   * and this is only the picking surface.
   *
   * `single` — exactly one agent is being chosen, so the field is a **select
   * trigger**: it shows the chosen agent's badge (or a placeholder), and
   * picking happens in a `Popover` with its own search rather than in a cloud
   * that spends the height of every agent the user has, inline, to say one
   * thing.
   */
  mode?: "single" | "multi"
  /**
   * Whether the list is being shown right now. Gates the fetch and resets the
   * search, so a dialog can pass its `open` and an inline host can pass `true`.
   */
  active?: boolean
  /** Single mode only: what the trigger says before an agent is chosen. */
  placeholder?: string
}

/**
 * The agent picker as a **form field**, without a `Dialog` around it.
 *
 * Extracted from `AgentSelectorDialog` so a form can hold the same picker its
 * siblings use, rather than a plain `SearchableSelect` of agent names — which
 * made one surface pick agents by a different vocabulary from every other
 * surface, with none of the colour identity people actually recognise agents
 * by.
 *
 * In `single` mode the field is a select trigger that opens the picker in a
 * `Popover` anchored to itself: one agent is one answer, and the answer is
 * worth one line of the form, not a scrolling cloud. Reopening it is the
 * trigger itself — there is no separate "Change" — and the `X` beside it (with
 * `allowDeselect`) is how the field is emptied.
 */
export function AgentSelectorList({
  mode = "multi",
  active = true,
  placeholder = "Select an agent",
  ...props
}: AgentSelectorListProps) {
  if (mode === "single") {
    return (
      <AgentSelectorField
        {...props}
        active={active}
        placeholder={placeholder}
      />
    )
  }
  return <AgentCloud {...props} mode="multi" active={active} />
}

/**
 * Single-select: the chosen agent as the pill it is everywhere else in the
 * product, in a control shaped like every other `Select` in the product, over
 * a `Popover` that does the choosing.
 *
 * A `Popover` and not a `Dialog`, even though the picking surface is exactly
 * the one `AgentSelectorDialog` shows: this field's whole point is to live
 * inside a form, and most of those forms are already dialogs (§2 "Disclosure
 * depth" — a value picker is anchored to the field it fills; `ListModelsButton`
 * and `UserAllowlistPicker` are the two surfaces that learned it first). It
 * returns one value into the field and closes.
 */
function AgentSelectorField({
  onSelect,
  selectedAgentId,
  workspaceId,
  excludeAgentIds,
  agents: externalAgents,
  allowDeselect = false,
  active,
  maxHeightClassName = "max-h-[240px]",
  disabled = false,
  placeholder,
}: AgentPickerBaseProps & { active: boolean; placeholder: string }) {
  const [pickerOpen, setPickerOpen] = useState(false)

  // Only to name the chosen agent on the trigger; the picker fetches the full
  // list when it opens, under the same query key.
  const { agents, isLoading } = useAgentOptions({
    workspaceId,
    excludeAgentIds,
    agents: externalAgents,
    enabled: active && !!selectedAgentId,
  })
  const selectedAgent = selectedAgentId
    ? agents.find((agent) => agent.id === selectedAgentId)
    : undefined

  return (
    <div className="flex items-center gap-2">
      <Popover open={pickerOpen} onOpenChange={setPickerOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            className={cn(
              "flex min-h-9 flex-1 items-center justify-between gap-2 rounded-md border border-input",
              "bg-transparent px-3 py-1.5 text-sm shadow-xs outline-none transition-[color,box-shadow]",
              "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
              "disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30 dark:hover:bg-input/50",
            )}
          >
            {selectedAgent ? (
              <AgentBadge
                agent={{
                  id: selectedAgent.id,
                  name: selectedAgent.name,
                  ui_color_preset: selectedAgent.colorPreset,
                }}
              />
            ) : (
              <span className="text-muted-foreground">
                {selectedAgentId && isLoading ? "Loading agent…" : placeholder}
              </span>
            )}
            <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
          </button>
        </PopoverTrigger>
        {/* As wide as the field it fills, so the pills wrap the way they will
            read once one of them is sitting on the trigger. */}
        <PopoverContent
          align="start"
          className="w-(--radix-popover-trigger-width) p-3"
        >
          <AgentCloud
            mode="single"
            onSelect={(agentId) => {
              onSelect(agentId)
              setPickerOpen(false)
            }}
            selectedAgentId={selectedAgentId}
            workspaceId={workspaceId}
            excludeAgentIds={excludeAgentIds}
            agents={externalAgents}
            active={pickerOpen}
            maxHeightClassName={maxHeightClassName}
          />
        </PopoverContent>
      </Popover>

      {/* The only way to empty the field — the trigger itself is how it is
          changed, so there is nothing else for a second button to do. */}
      {selectedAgent && allowDeselect && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-9 w-9 shrink-0 text-muted-foreground"
          aria-label={`Clear the selected agent ${selectedAgent.name}`}
          disabled={disabled}
          onClick={() => onSelect("")}
        >
          <X className="h-4 w-4" />
        </Button>
      )}
    </div>
  )
}

interface AgentSelectorDialogProps extends AgentPickerBaseProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Whether to close the dialog after selection */
  closeOnSelect?: boolean
}

/**
 * The picker as a dialog — the shape eight surfaces open from a button on a
 * page of their own. Nothing but the `Dialog` shell and the close-on-select
 * behaviour lives here; a form field uses `AgentSelectorList`, whose picker is
 * a `Popover`, so no form dialog ever grows a second dialog.
 */
export function AgentSelectorDialog({
  open,
  onOpenChange,
  onSelect,
  title = "Select Agent",
  closeOnSelect = true,
  ...cloudProps
}: AgentSelectorDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <AgentCloud
          {...cloudProps}
          mode="multi"
          active={open}
          onSelect={(agentId) => {
            onSelect(agentId)
            if (closeOnSelect) onOpenChange(false)
          }}
        />
      </DialogContent>
    </Dialog>
  )
}
