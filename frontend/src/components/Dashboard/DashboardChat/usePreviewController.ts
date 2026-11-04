import { useQuery } from "@tanstack/react-query"
import { useCallback, useMemo, useRef, useState } from "react"

import {
  AgentDashboardsService,
  type DashboardComponentRunResult,
  type DashboardTileDataPublic,
} from "@/client"
import type { ComponentView } from "@/components/Dashboard/AgentComponents/componentSpec"
import {
  componentErrorMessage,
  readComponentError,
} from "@/components/Dashboard/AgentComponents/tileConfig"
import type {
  ParamsRefusal,
  TileController,
} from "@/components/Dashboard/AgentComponents/tileController"
import useCustomToast from "@/hooks/useCustomToast"

function toTileData(
  result: DashboardComponentRunResult,
): DashboardTileDataPublic {
  return {
    data: result.value ?? null,
    fetched_at: result.fetched_at,
    notice: result.notice ?? null,
    params_resolved: result.params_resolved ?? {},
    stale: false,
  }
}

interface Options {
  agentId: string
  name: string
  skill: string | null
  /** From the component read; `null` until it is known. */
  view: ComponentView | null
}

/**
 * The `TileController` of the preview dialog (UI spec S6a) over the
 * agent-scoped routes (`AgentDashboardsService.run…` / `mint…Frame`, D-C9):
 * no stored params (the owner's choices live for the dialog only), no cache
 * and no page state. No `tile_key` is sent, so every preview shares the
 * component's one preview budget. A native view runs the component's source with the
 * current params; an html view runs nothing itself — its page calls its own
 * endpoints through the frame.
 */
export function usePreviewController({
  agentId,
  name,
  skill,
  view,
}: Options): TileController {
  const { showErrorToast } = useCustomToast()
  const [params, setParamsState] = useState<Record<string, unknown>>({})
  const [paramsRefusal, setParamsRefusal] = useState<ParamsRefusal | null>(null)
  const accepted = useRef<Record<string, unknown>>({})
  const paramsJson = JSON.stringify(params)
  const isNative = view !== null && view !== "html"

  const runQuery = useQuery({
    queryKey: ["agentComponentPreview", agentId, name, skill, paramsJson],
    queryFn: async () => {
      try {
        const result = await AgentDashboardsService.runAgentDashboardComponent({
          agentId,
          name,
          requestBody: { skill, params },
        })
        accepted.current = params
        setParamsRefusal(null)
        return toTileData(result)
      } catch (error) {
        const detail = readComponentError(error)
        if (detail?.code === "invalid_input") {
          // A refused value changes nothing: go back to the last accepted set.
          setParamsRefusal({
            field: detail.field?.replace(/^params\./, "") || null,
            message: detail.message,
          })
          setParamsState(accepted.current)
        } else if (detail?.code === "rate_limited") {
          showErrorToast(
            detail.retry_after_s
              ? `Refreshed too often — try again in ${detail.retry_after_s} s`
              : "Refreshed too often — try again in a moment",
          )
        }
        throw error
      }
    },
    enabled: isNative,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
    placeholderData: (previous) => previous,
  })

  const htmlData = useMemo<DashboardTileDataPublic>(
    () => ({ data: null, params_resolved: JSON.parse(paramsJson) }),
    [paramsJson],
  )

  const setParam = useCallback((key: string, value: unknown) => {
    setParamsState((current) => {
      const next = { ...current }
      if (value === null || value === undefined || value === "")
        delete next[key]
      else next[key] = value
      return next
    })
  }, [])

  const refusalIsBlocking =
    runQuery.isError &&
    readComponentError(runQuery.error)?.code === "invalid_input"

  return {
    data: isNative ? runQuery.data : view === "html" ? htmlData : undefined,
    isLoading: isNative && runQuery.isLoading,
    // A refused value keeps the last result on screen and says so in the
    // params row; only a failed run with nothing to show is a load error.
    loadError:
      isNative && runQuery.isError && !runQuery.data && !refusalIsBlocking
        ? runQuery.error
        : null,
    retryLoad: () => void runQuery.refetch(),
    storedParams: params,
    setParam,
    setParams: setParamsState,
    paramsRefusal,
    refresh: () => {
      if (isNative) void runQuery.refetch()
    },
    isRefreshing: isNative && runQuery.isFetching,
    runIssue:
      isNative && runQuery.isError && runQuery.data && !refusalIsBlocking
        ? componentErrorMessage(
            runQuery.error,
            "The preview could not refresh.",
          )
        : null,
    callEndpoint: async (endpoint, args) => {
      const result = await AgentDashboardsService.runAgentDashboardComponent({
        agentId,
        name,
        requestBody: { skill, endpoint, params: args },
      })
      return result.value ?? null
    },
    mintFrame: () =>
      AgentDashboardsService.mintAgentDashboardComponentFrame({
        agentId,
        name,
        requestBody: { skill },
      }),
    setPageState: () => {
      // A preview has no stored page state: the agent cannot highlight it.
    },
  }
}
