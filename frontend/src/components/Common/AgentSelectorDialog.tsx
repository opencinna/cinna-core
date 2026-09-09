import { useQuery } from "@tanstack/react-query"
import { Bot, Check, Search, X } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

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
import { cn } from "@/lib/utils"
import { getColorPreset } from "@/utils/colorPresets"

export interface AgentOption {
  id: string
  name: string
  colorPreset: string | null | undefined
}

interface AgentSelectorListProps {
  onSelect: (agentId: string) => void
  selectedAgentId?: string | null
  /** Pass a workspace ID to scope agents to that workspace. Omit for global. */
  workspaceId?: string | null
  /** Agent IDs to exclude from the list */
  excludeAgentIds?: string[]
  /** Override the fetched agents with a custom list (skips API call) */
  agents?: AgentOption[]
  title?: string
  /**
   * `multi` (default) — every agent stays on screen as a pill and the chosen
   * ones carry an outline; the caller keeps whatever selection model it likes
   * and this is only the picking surface.
   *
   * `single` — exactly one agent is being chosen, so once one is, the cloud
   * **collapses to that agent as a badge** with a Change beside it. A picker
   * that stays fully open after the choice is made says "keep choosing" in a
   * form whose next field is the real question, and it spends the height of
   * every agent the user has to say one thing.
   */
  mode?: "single" | "multi"
  /** Whether selecting an already-selected agent deselects it */
  allowDeselect?: boolean
  /**
   * Whether the list is being shown right now. Gates the fetch and resets the
   * search, so a dialog can pass its `open` and an inline host can pass `true`.
   */
  active?: boolean
  /** Height cap on the scrolling cloud. A form host has less room than a dialog. */
  maxHeightClassName?: string
  /** Frozen while a write this picker feeds is in flight. */
  disabled?: boolean
}

/**
 * The agent picker itself — search box + the colour-preset agent cloud —
 * without a `Dialog` around it.
 *
 * Extracted from `AgentSelectorDialog` so a **form** can hold the same picker
 * its siblings use. A dialog-based picker cannot be one of a form dialog's
 * fields (§2 "Disclosure depth", A2: a dialog does not open a dialog), and the
 * alternative — a plain `SearchableSelect` of agent names — made one surface
 * pick agents by a different vocabulary from every other surface, with none of
 * the colour identity people actually recognise agents by.
 */
export function AgentSelectorList({
  onSelect,
  selectedAgentId,
  workspaceId,
  excludeAgentIds,
  agents: externalAgents,
  mode = "multi",
  allowDeselect = false,
  active = true,
  maxHeightClassName = "max-h-[300px]",
  disabled = false,
}: AgentSelectorListProps) {
  const changeRef = useRef<HTMLButtonElement>(null)
  const [search, setSearch] = useState("")
  // Single-select only: is the cloud open. It starts open when nothing is
  // chosen yet and closes on a choice, so the ordinary path is "pick, and the
  // field shows what you picked" with no extra click either way.
  const [isPicking, setIsPicking] = useState(!selectedAgentId)

  // Reset search when the picker is shown
  useEffect(() => {
    if (active) setSearch("")
  }, [active])

  // Fetch agents only when no external list is provided
  const {
    data: agentsData,
    isLoading,
    isError,
    refetch,
  } = useQuery({
    queryKey: workspaceId ? ["agents", workspaceId] : ["allAgents"],
    queryFn: () =>
      AgentsService.readAgents({
        skip: 0,
        limit: 200,
        ...(workspaceId ? { userWorkspaceId: workspaceId } : {}),
      }),
    enabled: active && !externalAgents,
  })

  const allAgents: AgentOption[] = useMemo(() => {
    const source =
      externalAgents ??
      (agentsData?.data ?? []).map((a) => ({
        id: a.id,
        name: a.name,
        colorPreset: a.ui_color_preset,
      }))

    if (!excludeAgentIds?.length) return source
    const excludeSet = new Set(excludeAgentIds)
    return source.filter((a) => !excludeSet.has(a.id))
  }, [externalAgents, agentsData, excludeAgentIds])

  const filtered = useMemo(() => {
    if (!search.trim()) return allAgents
    const q = search.toLowerCase()
    return allAgents.filter((a) => a.name.toLowerCase().includes(q))
  }, [allAgents, search])

  // Choosing in single mode unmounts the cloud — including the button that was
  // just activated — so without this the keyboard user's focus falls to
  // `<body>` and the next Tab restarts at the top of the document, with no
  // announcement of what was chosen. Guarded against the first mount so a host
  // opening on a preselected agent does not steal focus from itself.
  const collapsedRef = useRef(false)
  useEffect(() => {
    const collapsed = mode === "single" && !isPicking && !!selectedAgentId
    if (collapsed && !collapsedRef.current) changeRef.current?.focus()
    collapsedRef.current = collapsed
  }, [mode, isPicking, selectedAgentId])

  // One-of-n is a radiogroup; the multi cloud is a plain group.
  const groupProps =
    mode === "single"
      ? ({ role: "radiogroup", "aria-label": "Agents" } as const)
      : ({ role: "group", "aria-label": "Agents" } as const)

  const handleSelect = (agentId: string) => {
    // In single mode a click is always a choice, even on the agent that is
    // already chosen: the cloud was reopened by "Change", so re-picking the
    // same one means "never mind" and must collapse rather than clear it.
    // Clearing is the X on the collapsed state — `allowDeselect` was written
    // for the multi cloud, where re-clicking is the only way to unpick.
    if (mode === "single") {
      onSelect(agentId)
      setIsPicking(false)
      return
    }
    if (allowDeselect && agentId === selectedAgentId) {
      onSelect("")
      return
    }
    onSelect(agentId)
  }

  const selectedAgent =
    mode === "single" && selectedAgentId
      ? allAgents.find((agent) => agent.id === selectedAgentId)
      : undefined

  // The chosen agent as the pill it is everywhere else in the product, with
  // the two verbs that make sense once a choice exists. `AgentBadge` is the
  // canonical pill, so the collapsed state and an agent named anywhere else
  // are visibly the same thing.
  if (selectedAgent && !isPicking) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <AgentBadge
          agent={{
            id: selectedAgent.id,
            name: selectedAgent.name,
            ui_color_preset: selectedAgent.colorPreset,
          }}
          size="md"
        />
        <Button
          ref={changeRef}
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          onClick={() => setIsPicking(true)}
        >
          Change
        </Button>
        {allowDeselect && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground"
            aria-label={`Clear the selected agent ${selectedAgent.name}`}
            disabled={disabled}
            onClick={() => {
              onSelect("")
              setIsPicking(true)
            }}
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>
    )
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
          have. Only for the self-fetching shape — a host that passed `agents`
          owns its own error. */}
      {isError && !externalAgents ? (
        <QueryErrorAlert
          error={null}
          fallback="Couldn't load your agents"
          onRetry={() => refetch()}
          compact
        />
      ) : isLoading && !externalAgents ? (
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

interface AgentSelectorDialogProps
  extends Omit<AgentSelectorListProps, "active" | "mode"> {
  open: boolean
  onOpenChange: (open: boolean) => void
  title?: string
  /** Whether to close the dialog after selection */
  closeOnSelect?: boolean
}

/**
 * The picker as a dialog — the shape eight surfaces already open from a
 * button. Nothing but the `Dialog` shell and the close-on-select behaviour
 * lives here; the picker itself is `AgentSelectorList`, so the two can never
 * drift into two different ways of choosing an agent.
 */
export function AgentSelectorDialog({
  open,
  onOpenChange,
  onSelect,
  title = "Select Agent",
  closeOnSelect = true,
  ...listProps
}: AgentSelectorDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <AgentSelectorList
          {...listProps}
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
