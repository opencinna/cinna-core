import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import type {
  AgentPublic,
  DashboardComponentPublic,
  UserDashboardPublic,
} from "@/client"
import { MessageInput } from "@/components/Chat/MessageInput"
import {
  type CollectedContext,
  collectTileContext,
} from "@/components/Dashboard/AgentComponents/bridgeHost"
import { componentsQueryKey } from "@/components/Dashboard/AgentComponents/ComponentPicker"
import { isRecord } from "@/components/Dashboard/AgentComponents/componentSpec"
import {
  getFocusedBlockId,
  getTileContext,
  registerDashboardChatPanel,
  useDashboardFocus,
  useFocusedTileContext,
  useTileContexts,
} from "@/components/Dashboard/AgentComponents/dashboardFocus"
import { agentComponentQueryKey } from "@/components/Dashboard/AgentComponents/useAgentComponent"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "@/components/ui/sheet"
import { cn } from "@/lib/utils"
import { ContextChip } from "./ContextChip"
import { DashboardAgentChat, DashboardChatHeader } from "./DashboardAgentChat"
import { nextSeed, type Seed } from "./DashboardChatConversation"
import {
  buildDashboardContext,
  CONTEXT_MAX_CHARS,
  focusedView,
} from "./dashboardContext"

export const DASHBOARD_CHAT_TOGGLE_ID = "dashboard-chat-toggle"

/** How long the page has to answer `cinna/collect-context` (plan §8). */
const COLLECT_TIMEOUT_MS = 500
/** After a data delivery, give the page this long to draw before asking. */
const COLLECT_AFTER_DATA_MS = 300

interface Props {
  dashboard: UserDashboardPublic
  agents: AgentPublic[]
  open: boolean
  onOpenChange: (open: boolean) => void
  /** ≥ lg: a docked column; below: a Sheet. */
  docked: boolean
  isEditMode: boolean
}

function componentContextText(
  component: DashboardComponentPublic | undefined,
): string | null {
  const json = component?.component_json
  return isRecord(json) && typeof json.context === "string" && json.context
    ? json.context
    : null
}

/**
 * The dashboard chat (UI spec S5, plan §8): the focused tile's agent, asked
 * about what is on screen without leaving the dashboard. One session per
 * (dashboard, agent); focusing another agent's tile switches to its session,
 * and each agent's chat stays mounted so its draft and a running turn survive.
 */
export function DashboardChatPanel({
  dashboard,
  agents,
  open,
  onOpenChange,
  docked,
  isEditMode,
}: Props) {
  const dashboardId = dashboard.id
  const queryClient = useQueryClient()
  const tiles = useTileContexts(dashboardId)
  const focused = useFocusedTileContext(dashboardId)
  const shown = open && !isEditMode

  const agentsById = useMemo(
    () => new Map(agents.map((a) => [a.id, a])),
    [agents],
  )
  // A dashboard whose tiles all share one agent needs no focus to pick it.
  const soleAgentId = useMemo(() => {
    const ids = new Set(
      (dashboard.blocks ?? []).map((b) => b.agent_id).filter(Boolean),
    )
    return ids.size === 1 ? (Array.from(ids)[0] ?? null) : null
  }, [dashboard.blocks])
  const activeAgentId = focused?.agentId ?? soleAgentId
  const activeAgent = activeAgentId
    ? (agentsById.get(activeAgentId) ?? null)
    : null

  const [hasOpened, setHasOpened] = useState(shown)
  useEffect(() => {
    if (shown) setHasOpened(true)
  }, [shown])
  const [visited, setVisited] = useState<string[]>([])
  useEffect(() => {
    if (activeAgent && hasOpened)
      setVisited((v) =>
        v.includes(activeAgent.id) ? v : [...v, activeAgent.id],
      )
  }, [activeAgent, hasOpened])

  // ── Seeds (a page's ui/message, Ask to fix): fill, never send ─────────
  const [seeds, setSeeds] = useState<Record<string, Seed>>({})
  const seedAgent = useCallback((agentId: string, text: string) => {
    setSeeds((current) => ({ ...current, [agentId]: nextSeed(text) }))
  }, [])

  const openRef = useRef(onOpenChange)
  openRef.current = onOpenChange
  useEffect(() => {
    // In edit mode there is no chat: tiles hide Ask and answer `ui/message`
    // with "No dashboard chat here."
    if (isEditMode) return
    return registerDashboardChatPanel(dashboardId, {
      open: () => openRef.current(true),
      seedComposer: (text, blockId) => {
        const agentId = getTileContext(dashboardId, blockId)?.agentId
        if (agentId) seedAgent(agentId, text)
        openRef.current(true)
      },
    })
  }, [dashboardId, isEditMode, seedAgent])

  // ── Collect-context from the focused html page ─────────────────────────
  const [collected, setCollected] = useState<{
    blockId: string
    value: CollectedContext | null
  } | null>(null)
  const collect = useCallback(() => {
    const blockId = getFocusedBlockId(dashboardId)
    if (!blockId || getTileContext(dashboardId, blockId)?.view !== "html")
      return
    void collectTileContext(blockId, COLLECT_TIMEOUT_MS).then((value) => {
      // An answer that arrives after focus moved is about the wrong tile.
      if (getFocusedBlockId(dashboardId) !== blockId) return
      // A timeout keeps the last good answer, so the context (and its hash)
      // does not flip between the page's words and the tile's on a slow tick.
      setCollected((current) =>
        value || current?.blockId !== blockId ? { blockId, value } : current,
      )
    })
  }, [dashboardId])
  const focusedBlockId = focused?.blockId ?? null
  const focusedFetchedAt = focused?.fetchedAt ?? null
  const focusedIsHtml = focused?.view === "html"
  useEffect(() => {
    // Nobody can send while the panel is hidden: do not ask the page.
    if (!shown || !focusedBlockId || !focusedIsHtml) return
    void focusedFetchedAt
    const timer = window.setTimeout(collect, COLLECT_AFTER_DATA_MS)
    return () => window.clearTimeout(timer)
  }, [shown, focusedBlockId, focusedIsHtml, focusedFetchedAt, collect])

  const collectedValue =
    collected && collected.blockId === focusedBlockId ? collected.value : null
  const view = useMemo(
    () => (focused ? focusedView(focused, collectedValue) : null),
    [focused, collectedValue],
  )

  // ── The chip: removed for the next message, back on any focus ──────────
  // `focusSeq` moves on every focus request, the already-focused tile's too.
  const { focusSeq } = useDashboardFocus(dashboardId)
  const [removedFor, setRemovedFor] = useState<string | null>(null)
  useEffect(() => {
    void focusSeq
    setRemovedFor(null)
  }, [focusSeq])
  const contextOn = !!focused && !!view && removedFor !== focused.blockId

  const tilesRef = useRef(tiles)
  tilesRef.current = tiles

  const chatProps = (agentId: string) => {
    const mine = contextOn && focused?.agentId === agentId
    return {
      buildContext: (explained: Set<string>) => {
        if (!mine || !focused || !view) return undefined
        const component = queryClient.getQueryData<DashboardComponentPublic>(
          agentComponentQueryKey(
            focused.agentId,
            focused.component,
            focused.skill,
          ),
        )
        const componentContext = componentContextText(component)
        const text = buildDashboardContext({
          dashboard: { id: dashboard.id, name: dashboard.name },
          focused,
          view,
          componentContext:
            componentContext && !explained.has(focused.component)
              ? componentContext
              : null,
          tiles,
          agentName: (id) => agentsById.get(id)?.name || "another agent",
        })
        return text.length <= CONTEXT_MAX_CHARS ? text : undefined
      },
      onComposerFocus: collect,
      onMessageSent: () => setRemovedFor(null),
      onTurnEnd: (sessionId: string) => {
        // The turn may have edited the agent's own component.json: re-read
        // its specs (view, params, prompts), its component list, and the
        // reference cards this session printed (a new component was `gone`).
        queryClient.invalidateQueries({
          queryKey: ["agentDashboardComponent", agentId],
        })
        queryClient.invalidateQueries({ queryKey: componentsQueryKey(agentId) })
        queryClient.invalidateQueries({
          predicate: (query) =>
            query.queryKey[0] === "componentRef" &&
            query.queryKey[2] === sessionId,
        })
        // Plan §8: the agent's tiles on screen with after_turn refresh; the
        // others re-read what the turn may have changed.
        for (const tile of tilesRef.current) {
          if (tile.agentId !== agentId) continue
          tile.refresh(tile.afterTurn && tile.visible ? "after_turn" : "reread")
        }
      },
      starters: focused?.agentId === agentId ? focused.prompts : [],
      chip:
        mine && focused && view ? (
          <ContextChip
            tile={focused}
            view={view}
            onRemove={() => setRemovedFor(focused.blockId)}
          />
        ) : null,
    }
  }

  const hide = () => {
    onOpenChange(false)
    // The panel unmounts its focus target: hand focus back to the toggle.
    window.setTimeout(
      () => document.getElementById(DASHBOARD_CHAT_TOGGLE_ID)?.focus(),
      0,
    )
  }

  const body = hasOpened ? (
    <>
      {!activeAgent && (
        <div className="flex min-h-0 flex-1 flex-col">
          <DashboardChatHeader agent={null} onHide={hide} />
          <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">
            Select a tile to ask its agent.
          </div>
          <fieldset disabled className="min-w-0">
            <MessageInput
              onSend={() => undefined}
              placeholder="Select a tile to ask its agent."
              compact
            />
          </fieldset>
        </div>
      )}
      {visited.map((agentId) => {
        const agent = agentsById.get(agentId)
        if (!agent) return null
        return (
          <DashboardAgentChat
            key={agentId}
            dashboardId={dashboardId}
            agent={agent}
            active={agentId === activeAgent?.id}
            visible={shown && agentId === activeAgent?.id}
            seed={seeds[agentId]}
            onSeed={(text) => seedAgent(agentId, text)}
            onHide={hide}
            {...chatProps(agentId)}
          />
        )
      })}
    </>
  ) : null

  if (docked) {
    return (
      <aside
        aria-label="Dashboard chat"
        className={cn(
          "w-[320px] shrink-0 flex-col border-l bg-background xl:w-[360px]",
          shown ? "flex" : "hidden",
        )}
      >
        {body}
      </aside>
    )
  }

  return (
    <Sheet open={shown} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="flex w-[360px] max-w-full flex-col gap-0 p-0 [&>button:last-child]:hidden"
      >
        <SheetTitle className="sr-only">Dashboard chat</SheetTitle>
        <SheetDescription className="sr-only">
          Ask the focused tile's agent about this dashboard.
        </SheetDescription>
        {body}
      </SheetContent>
    </Sheet>
  )
}
