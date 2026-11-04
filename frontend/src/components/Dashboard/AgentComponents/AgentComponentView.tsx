import {
  type KeyboardEvent,
  type MouseEvent,
  useCallback,
  useMemo,
  useState,
} from "react"

import type { DashboardComponentPublic } from "@/client"
import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import type { BridgeInit } from "./bridgeHost"
import {
  type ComponentView,
  inferView,
  isRecord,
  readParamSpecs,
  readSize,
  readView,
} from "./componentSpec"
import type { PageContext } from "./dashboardFocus"
import { HtmlFrameView } from "./HtmlFrameView"
import {
  clearHighlight,
  stepHighlight,
  useTileHighlight,
} from "./highlightStore"
import { NativeListView, readListItems } from "./NativeListView"
import { NativeMetricsView, readMetrics } from "./NativeMetricsView"
import { NativeTableView, readTable } from "./NativeTableView"
import { ParamsRow } from "./ParamsRow"
import { CredentialsSentence, TileFooter } from "./TileFooter"
import { componentErrorMessage } from "./tileConfig"
import type { TileController } from "./tileController"

export interface AgentComponentViewProps {
  /** `preview` (P7's dialog) has no Ask, no focus and no highlights. */
  mode: "tile" | "preview"
  /** The block id for a tile; any stable key for a preview. */
  tileKey: string
  agent: { id: string; name: string }
  component: DashboardComponentPublic | undefined
  /** The component itself could not be read (after the env-start retries). */
  componentError: unknown
  controller: TileController
  title: string
  frameQueryKey: readonly unknown[]
  onFocusTile?: () => void
  /** Re-read the component after its read failed. */
  onRetryComponent?: () => void
  onAsk?: () => void
  onSelectionChange?: (ids: string[]) => void
  onModelContext?: (context: PageContext) => void
  onMessage?: (text: string) => boolean
  confirmLink: (url: string) => Promise<boolean> | null
}

function isEmpty(view: ComponentView, data: unknown): boolean {
  if (data === null || data === undefined) return true
  if (view === "html") return false
  if (view === "markdown")
    return !(
      isRecord(data) &&
      typeof data.markdown === "string" &&
      data.markdown.trim()
    )
  if (view === "table") return readTable(data).rows.length === 0
  return readListItems(data).length === 0
}

function BodySkeleton({ view }: { view: ComponentView | null }) {
  if (view === "html")
    return <Skeleton className="h-full w-full rounded-none" />
  if (view === "metrics") {
    return (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-2 p-2">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-14" />
        ))}
      </div>
    )
  }
  return (
    <div className="space-y-2 p-3">
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-2/3" />
    </div>
  )
}

/** Clicks on a control or a link inside the body do not take tile focus. */
function isControlClick(event: MouseEvent<HTMLElement>) {
  return !!(event.target as HTMLElement).closest(
    "a, button, input, select, textarea, [role=combobox]",
  )
}

/**
 * An agent component, drawn: params row · body · footer (UI spec S1). The
 * header strip belongs to `DashboardBlock`. Shared by the dashboard tile and
 * P7's preview dialog through `mode` and a `TileController`.
 */
export function AgentComponentView({
  mode,
  tileKey,
  agent,
  component,
  componentError,
  controller,
  title,
  frameQueryKey,
  onFocusTile,
  onRetryComponent,
  onAsk,
  onSelectionChange,
  onModelContext,
  onMessage,
  confirmLink,
}: AgentComponentViewProps) {
  const { data: tile } = controller
  const view = useMemo(
    () => readView(component) ?? inferView(tile?.data),
    [component, tile?.data],
  )
  const specs = useMemo(() => readParamSpecs(component), [component])
  const storeHighlight = useTileHighlight(tileKey)
  const highlight = mode === "tile" ? storeHighlight : { ids: [], current: -1 }
  const highlightedIds = useMemo(() => new Set(highlight.ids), [highlight.ids])
  const currentId =
    highlight.current >= 0 ? (highlight.ids[highlight.current] ?? null) : null
  const [selection, setSelection] = useState<ReadonlySet<string>>(
    () => new Set(),
  )

  const toggle = useCallback(
    (id: string) => {
      const next = new Set(selection)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      setSelection(next)
      onSelectionChange?.(Array.from(next))
      // A new owner pick replaces what the agent pointed at (plan §7).
      clearHighlight(tileKey)
      onFocusTile?.()
    },
    [onFocusTile, onSelectionChange, selection, tileKey],
  )

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && view !== "html") clearHighlight(tileKey)
  }

  const init: BridgeInit = {
    component: component?.name ?? "",
    title,
    params: tile?.params_resolved ?? {},
    size: readSize(component),
  }

  const missing = tile?.missing_credentials ?? []

  const body = (() => {
    if (controller.loadError) {
      return (
        <div className="p-2">
          <QueryErrorAlert
            compact
            error={{
              message: componentErrorMessage(
                controller.loadError,
                "The tile could not be read.",
              ),
            }}
            fallback="Couldn't load this tile."
            onRetry={controller.retryLoad}
          />
        </div>
      )
    }
    if (!view && componentError) {
      return (
        <div className="p-2">
          <QueryErrorAlert
            compact
            error={{
              message: componentErrorMessage(
                componentError,
                "The component could not be read.",
              ),
            }}
            fallback="Couldn't load this tile."
            onRetry={onRetryComponent}
          />
        </div>
      )
    }
    if (controller.isLoading || !view) return <BodySkeleton view={view} />
    if (
      missing.length > 0 &&
      (tile?.data === null || tile?.data === undefined)
    ) {
      return (
        <div className="flex h-full items-center justify-center p-4 text-center text-sm">
          <CredentialsSentence agentId={agent.id} names={missing} />
        </div>
      )
    }
    if (view === "html") {
      if (!component) return <BodySkeleton view="html" />
      return (
        <HtmlFrameView
          blockId={tileKey}
          frameQueryKey={frameQueryKey}
          controller={controller}
          init={init}
          onFrameFocus={() => onFocusTile?.()}
          onModelContext={(context) => onModelContext?.(context)}
          onMessage={(text) => onMessage?.(text) ?? false}
          confirmLink={confirmLink}
        />
      )
    }
    if (isEmpty(view, tile?.data)) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-1 p-4 text-center text-sm text-muted-foreground">
          <p>Nothing to show yet.</p>
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={() => controller.refresh("manual")}
            disabled={controller.isRefreshing}
          >
            Refresh
          </Button>
        </div>
      )
    }
    const data = tile?.data
    const rowProps = {
      selection,
      onToggle: toggle,
      highlighted: highlightedIds,
      currentId,
    }
    switch (view) {
      case "markdown":
        return (
          <MarkdownRenderer
            content={
              isRecord(data) && typeof data.markdown === "string"
                ? data.markdown
                : ""
            }
            className="prose prose-sm dark:prose-invert max-w-none break-words px-3 py-2"
          />
        )
      case "table":
        return <NativeTableView table={readTable(data)} {...rowProps} />
      case "list":
        return <NativeListView items={readListItems(data)} {...rowProps} />
      case "metrics":
        return <NativeMetricsView metrics={readMetrics(data)} />
    }
  })()

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: Escape clears the agent's highlights from anywhere in the tile; the rows and buttons inside carry their own keyboard handling.
    <div className="flex h-full min-h-0 flex-col" onKeyDown={onKeyDown}>
      <ParamsRow
        specs={specs}
        stored={controller.storedParams}
        resolved={tile?.params_resolved ?? {}}
        refusal={controller.paramsRefusal}
        onChange={controller.setParam}
      />
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: focusing a tile for the chat is a pointer convenience; Ask and the frame's own focus are the keyboard routes. */}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: as above. */}
      <div
        className="min-h-0 flex-1 overflow-auto"
        onClick={(event) => {
          if (view !== "html" && !isControlClick(event)) onFocusTile?.()
        }}
      >
        {body}
      </div>
      <TileFooter
        agentId={agent.id}
        agentName={agent.name}
        data={tile}
        isHtml={view === "html"}
        isRefreshing={controller.isRefreshing}
        runIssue={controller.runIssue}
        highlight={highlight}
        onStep={(delta) => stepHighlight(tileKey, delta)}
        onRefresh={() => controller.refresh("manual")}
        onAsk={mode === "tile" ? onAsk : undefined}
        onFocusTile={onFocusTile}
      />
    </div>
  )
}
