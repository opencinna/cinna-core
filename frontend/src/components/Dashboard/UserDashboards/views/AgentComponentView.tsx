import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef } from "react"

import type { AgentPublic, UserDashboardBlockPublic } from "@/client"
import { AgentComponentView } from "@/components/Dashboard/AgentComponents/AgentComponentView"
import {
  durationMs,
  readPrompts,
  readRefreshDefaults,
  readView,
} from "@/components/Dashboard/AgentComponents/componentSpec"
import {
  focusTile,
  getDashboardChatPanel,
  getTileContext,
  type PageContext,
  patchTileContext,
  publishTileContext,
  removeTileContext,
  useHasDashboardChat,
} from "@/components/Dashboard/AgentComponents/dashboardFocus"
import { clearHighlight } from "@/components/Dashboard/AgentComponents/highlightStore"
import { useOpenLinkConfirm } from "@/components/Dashboard/AgentComponents/OpenLinkConfirm"
import { readTileConfig } from "@/components/Dashboard/AgentComponents/tileConfig"
import { useAgentComponent } from "@/components/Dashboard/AgentComponents/useAgentComponent"
import {
  tileDataQueryKey,
  useAgentComponentTile,
} from "@/components/Dashboard/AgentComponents/useAgentComponentTile"

const MIN_WHILE_OPEN_MS = 60_000

interface Props {
  block: UserDashboardBlockPublic
  agent: AgentPublic
  dashboardId: string
  title: string
}

/** The tile's display title: `block.title || component title || component name`. */
export function agentComponentTileTitle(
  block: UserDashboardBlockPublic,
  componentTitleText: string | null,
): string {
  return (
    block.title ||
    componentTitleText ||
    readTileConfig(block).component ||
    "Component"
  )
}

function sameIds(a: string[], b: string[]) {
  return a.length === b.length && a.every((id, i) => id === b[i])
}

/**
 * An `agent_component` block on a dashboard (UI spec S1): wires the block
 * routes, the refresh driver, the socket events, tile focus and the chat
 * panel seam into the shared `AgentComponentView`.
 */
export function AgentComponentTile({
  block,
  agent,
  dashboardId,
  title,
}: Props) {
  const queryClient = useQueryClient()
  const config = useMemo(() => readTileConfig(block), [block])
  const componentQuery = useAgentComponent(
    agent.id,
    config.component,
    config.skill,
  )
  const component = componentQuery.data
  const defaults = readRefreshDefaults(component)
  const override = config.refresh.while_open_every
  const declaredMs =
    override === "manual"
      ? null
      : durationMs(override || defaults.while_open_every)
  // Never faster than a minute, whatever the component or an old override says.
  const intervalMs =
    declaredMs === null ? null : Math.max(MIN_WHILE_OPEN_MS, declaredMs)
  const afterTurn = config.refresh.after_turn ?? defaults.after_turn

  const elementRef = useRef<HTMLDivElement | null>(null)
  const { controller, visible } = useAgentComponentTile({
    dashboardId,
    block,
    elementRef,
    ready: componentQuery.isSuccess,
    intervalMs,
  })

  const hasChat = useHasDashboardChat(dashboardId)
  const { confirmLink, dialog } = useOpenLinkConfirm(title)
  const focus = useCallback(
    () => focusTile(dashboardId, block.id),
    [dashboardId, block.id],
  )

  // Publish what this tile shows, for the dashboard chat (P7).
  const { refresh } = controller
  const tileData = controller.data
  useEffect(() => {
    const previous = getTileContext(dashboardId, block.id)
    publishTileContext({
      blockId: block.id,
      dashboardId,
      agentId: agent.id,
      title,
      component: config.component,
      skill: config.skill,
      view: readView(component),
      params: tileData?.params_resolved ?? {},
      selection: previous?.selection ?? [],
      pageContext: previous?.pageContext ?? null,
      fetchedAt: tileData?.fetched_at ?? null,
      prompts: readPrompts(component),
      afterTurn,
      visible,
      refresh: (reason) => {
        if (reason === "after_turn") refresh("after_turn")
        else
          queryClient.invalidateQueries({
            queryKey: tileDataQueryKey(dashboardId, block.id),
          })
      },
    })
  }, [
    agent.id,
    block.id,
    component,
    config.component,
    config.skill,
    dashboardId,
    afterTurn,
    queryClient,
    refresh,
    tileData,
    title,
    visible,
  ])
  useEffect(
    () => () => removeTileContext(dashboardId, block.id),
    [dashboardId, block.id],
  )

  const onSelectionChange = useCallback(
    (ids: string[]) =>
      patchTileContext(dashboardId, block.id, { selection: ids }),
    [dashboardId, block.id],
  )

  const onModelContext = useCallback(
    (context: PageContext) => {
      const previous =
        getTileContext(dashboardId, block.id)?.pageContext?.selection ?? []
      // A page's new, non-empty selection is the owner picking: it replaces
      // what the agent highlighted (plan §7).
      if (
        context.selection.length > 0 &&
        !sameIds(previous, context.selection)
      ) {
        clearHighlight(block.id)
      }
      patchTileContext(dashboardId, block.id, {
        pageContext: context,
        selection: context.selection,
      })
    },
    [dashboardId, block.id],
  )

  const onMessage = useCallback(
    (text: string) => {
      const panel = getDashboardChatPanel(dashboardId)
      if (!panel) return false
      focus()
      panel.seedComposer(text, block.id)
      return true
    },
    [dashboardId, block.id, focus],
  )

  const onAsk = hasChat
    ? () => {
        focus()
        getDashboardChatPanel(dashboardId)?.open(block.id)
      }
    : undefined

  // The env-start retries are the query's own: `isError` means they ran out.
  const componentError = componentQuery.isError ? componentQuery.error : null

  return (
    <div ref={elementRef} className="h-full">
      <AgentComponentView
        mode="tile"
        tileKey={block.id}
        agent={agent}
        component={component}
        componentError={componentError}
        controller={controller}
        title={title}
        frameQueryKey={["dashboardTileFrame", dashboardId, block.id]}
        onFocusTile={focus}
        onRetryComponent={() => void componentQuery.refetch()}
        onAsk={onAsk}
        onSelectionChange={onSelectionChange}
        onModelContext={onModelContext}
        onMessage={onMessage}
        confirmLink={confirmLink}
      />
      {dialog}
    </div>
  )
}
