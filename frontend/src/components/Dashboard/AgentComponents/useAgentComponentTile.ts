import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react"

import {
  type DashboardTileDataPublic,
  type DashboardTilePageStateRequest,
  DashboardTilesService,
  OpenAPI,
  type UserDashboardBlockPublic,
} from "@/client"
import { parseTimestamp } from "@/components/Common/RelativeTime"
import useCustomToast from "@/hooks/useCustomToast"
import { useEventSubscription } from "@/hooks/useEventBus"
import type { EventData } from "@/services/eventService"
import { setHighlight } from "./highlightStore"
import {
  componentErrorMessage,
  readComponentError,
  readTileConfig,
} from "./tileConfig"
import type {
  ParamsRefusal,
  RefreshReason,
  TileController,
} from "./tileController"
import { useTileRefreshDriver } from "./useTileRefreshDriver"

let lastPageStateSeq = 0

/**
 * Monotonic across tab reloads (the server ignores a lower `seq` than the one
 * it holds): microseconds since the epoch, bumped past the last one issued.
 */
function nextPageStateSeq(): number {
  lastPageStateSeq = Math.max(lastPageStateSeq + 1, Date.now() * 1000)
  return lastPageStateSeq
}

/**
 * The generated client cannot set `keepalive`, which is what lets a request
 * outlive a closing tab; this is the same PUT through `fetch`.
 */
function sendPageStateKeepalive(
  dashboardId: string,
  blockId: string,
  body: DashboardTilePageStateRequest,
): void {
  const token = localStorage.getItem("access_token")
  void fetch(
    `${OpenAPI.BASE}/api/v1/dashboards/${dashboardId}/blocks/${blockId}/component/page-state`,
    {
      method: "PUT",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
    },
  ).catch(() => {
    // Best effort: the server also stops counting a page after 15 min.
  })
}

function isOlder(
  next: DashboardTileDataPublic,
  current: DashboardTileDataPublic | undefined,
): boolean {
  const a = next.fetched_at ? parseTimestamp(next.fetched_at) : null
  const b = current?.fetched_at ? parseTimestamp(current.fetched_at) : null
  return !!a && !!b && a.getTime() < b.getTime()
}

/** The house rhythm for re-reading what is on screen. */
const REREAD_MS = 30_000

export function tileDataQueryKey(dashboardId: string, blockId: string) {
  return ["dashboardTileData", dashboardId, blockId] as const
}

interface Options {
  dashboardId: string
  block: UserDashboardBlockPublic
  elementRef: RefObject<HTMLElement | null>
  /** The component is readable (its environment runs): refreshes may start. */
  ready: boolean
  /** Effective `while_open_every` in ms (override, else the component's), or `null`. */
  intervalMs: number | null
}

function rateLimitSentence(retryAfter: number | null | undefined): string {
  return retryAfter
    ? `Refreshed too often — try again in ${retryAfter} s`
    : "Refreshed too often — try again in a moment"
}

/**
 * A dashboard tile's data over the block routes (`DashboardTilesService`):
 * cached data first, the refresh driver, auto-saved params, the page's
 * endpoint calls, its frame and page state, and the two socket events.
 */
export function useAgentComponentTile({
  dashboardId,
  block,
  elementRef,
  ready,
  intervalMs,
}: Options): { controller: TileController; visible: boolean } {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const blockId = block.id
  const dataKey = useMemo(
    () => tileDataQueryKey(dashboardId, blockId),
    [dashboardId, blockId],
  )
  const ids = { dashboardId, blockId }

  // The stored set is the block's (`config.params`), which the agent can
  // change too (`dashboard_tile_updated` re-reads the block). A change the
  // owner just made is overlaid until the block catches up or the write fails.
  const serverParamsJson = JSON.stringify(readTileConfig(block).params)
  const serverParams = useMemo(
    () => JSON.parse(serverParamsJson) as Record<string, unknown>,
    [serverParamsJson],
  )
  const [optimistic, setOptimistic] = useState<Record<string, unknown> | null>(
    null,
  )
  // PUTs in flight: while one runs, a block re-read that does not yet match
  // the overlay is an older state (a quick A-then-B change must not flash A).
  const paramsInFlight = useRef(0)
  useEffect(() => {
    setOptimistic((overlay) =>
      overlay === null ||
      paramsInFlight.current === 0 ||
      JSON.stringify(overlay) === serverParamsJson
        ? null
        : overlay,
    )
  }, [serverParamsJson])
  const storedParams = optimistic ?? serverParams
  const storedRef = useRef(storedParams)
  storedRef.current = storedParams
  const [paramsRefusal, setParamsRefusal] = useState<ParamsRefusal | null>(null)
  const [runIssue, setRunIssue] = useState<string | null>(null)

  const onRunError = useCallback(
    (error: unknown, reason: RefreshReason | "params") => {
      const detail = readComponentError(error)
      if (detail?.code === "rate_limited") {
        if (reason === "manual" || reason === "params") {
          showErrorToast(rateLimitSentence(detail.retry_after_s))
        }
        return
      }
      setRunIssue(componentErrorMessage(error, "The tile could not refresh."))
    },
    [showErrorToast],
  )

  const onFresh = useCallback(
    (data: DashboardTileDataPublic) => {
      // A re-read already in flight must not land on top of this result.
      void queryClient.cancelQueries({ queryKey: dataKey })
      queryClient.setQueryData<DashboardTileDataPublic>(dataKey, (current) =>
        isOlder(data, current) ? current : data,
      )
      setRunIssue(null)
    },
    [queryClient, dataKey],
  )

  const refreshMutation = useMutation({
    mutationFn: (reason: RefreshReason) =>
      DashboardTilesService.refreshDashboardTile({
        ...ids,
        requestBody: { reason },
      }),
    onSuccess: onFresh,
    onError: onRunError,
  })

  const pageStateChain = useRef<Promise<unknown>>(Promise.resolve())

  // Overlapping PUTs can settle out of order: only the latest call's answer counts.
  const latestParams = useRef<Record<string, unknown> | null>(null)
  const paramsMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      DashboardTilesService.setDashboardTileParams({
        ...ids,
        requestBody: { params },
      }),
    onMutate: (params) => {
      paramsInFlight.current += 1
      latestParams.current = params
      setOptimistic(params)
    },
    onSuccess: (data, params) => {
      if (params !== latestParams.current) return
      setParamsRefusal(null)
      onFresh(data)
      queryClient.invalidateQueries({
        queryKey: ["userDashboard", dashboardId],
      })
    },
    onSettled: () => {
      paramsInFlight.current -= 1
    },
    onError: (error, params) => {
      if (params !== latestParams.current) return
      setOptimistic(null)
      const detail = readComponentError(error)
      if (detail?.code === "invalid_input") {
        setParamsRefusal({
          field: detail.field?.replace(/^params\./, "") || null,
          message: detail.message,
        })
        return
      }
      onRunError(error, "params")
    },
  })

  const refresh = refreshMutation.mutate
  const visible = useTileRefreshDriver({
    elementRef,
    ready,
    intervalMs,
    onRefresh: refresh,
  })

  const dataQuery = useQuery({
    queryKey: dataKey,
    queryFn: () => DashboardTilesService.getDashboardTileData(ids),
    refetchInterval: visible ? REREAD_MS : false,
  })

  // `dashboard_tile_updated {dashboard_id, block_id}` and
  // `dashboard_tile_highlight {dashboard_id, block_id, ids}` (plan §5.3).
  const onTileEvent = useCallback(
    (event: EventData) => {
      const meta = event.meta ?? {}
      if (meta.block_id !== blockId) return
      if (event.type === "dashboard_tile_updated") {
        queryClient.invalidateQueries({ queryKey: dataKey })
        // The agent may have changed the params: re-read the block's stored set.
        queryClient.invalidateQueries({
          queryKey: ["userDashboard", dashboardId],
        })
      } else if (event.type === "dashboard_tile_highlight") {
        setHighlight(blockId, meta.ids)
      }
    },
    [blockId, dashboardId, dataKey, queryClient],
  )
  useEventSubscription("dashboard_tile_updated", onTileEvent)
  useEventSubscription("dashboard_tile_highlight", onTileEvent)

  const setParams = paramsMutation.mutate
  const setParam = useCallback(
    (name: string, value: unknown) => {
      const next = { ...storedRef.current }
      if (value === null || value === undefined || value === "")
        delete next[name]
      else next[name] = value
      setParams(next)
    },
    [setParams],
  )

  const controller: TileController = {
    data: dataQuery.data,
    isLoading: dataQuery.isLoading,
    loadError: dataQuery.isError ? dataQuery.error : null,
    retryLoad: () => {
      void dataQuery.refetch()
    },
    storedParams,
    setParam,
    setParams,
    paramsRefusal,
    refresh,
    isRefreshing: refreshMutation.isPending || paramsMutation.isPending,
    runIssue,
    callEndpoint: async (endpoint, args) => {
      const result = await DashboardTilesService.callDashboardTileEndpoint({
        ...ids,
        requestBody: { endpoint, params: args },
      })
      return result.value ?? null
    },
    mintFrame: () => DashboardTilesService.mintDashboardTileFrame(ids),
    setPageState: (state, options) => {
      const body = { ...state, seq: nextPageStateSeq() }
      if (options?.keepalive) {
        // The tab may be going away: this write must not wait in the queue.
        sendPageStateKeepalive(dashboardId, blockId, body)
        return
      }
      // One queue per tile so the writes leave in order; `seq` also lets the
      // server drop one that overtakes another on the way.
      pageStateChain.current = pageStateChain.current
        .then(() =>
          DashboardTilesService.setDashboardTilePageState({
            ...ids,
            requestBody: body,
          }),
        )
        .catch(() => {
          // Readiness is advisory: the agent hears `highlighted: false` until
          // the next write lands. Nothing for the owner to act on.
        })
    },
  }

  return { controller, visible }
}
