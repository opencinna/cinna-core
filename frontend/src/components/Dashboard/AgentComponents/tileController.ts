import type {
  DashboardFramePublic,
  DashboardTileDataPublic,
  DashboardTileRefreshRequest,
} from "@/client"

export type RefreshReason = DashboardTileRefreshRequest["reason"]

export interface ParamsRefusal {
  field: string | null
  message: string
}

/**
 * What `AgentComponentView` needs from wherever a component's data comes from.
 *
 * The dashboard tile (`useAgentComponentTile`) drives the block routes
 * (`DashboardTilesService`); P7's preview dialog implements the same shape
 * over the agent-scoped routes (`AgentDashboardsService.run…/mint…`), so one
 * view draws both (UI spec S1/S6a, `mode: "tile" | "preview"`).
 */
export interface TileController {
  data: DashboardTileDataPublic | undefined
  isLoading: boolean
  /** A failed read of the cached data; distinct from "no data yet". */
  loadError: unknown
  retryLoad: () => void

  /** The tile's stored param set (keys left out resolve to their default). */
  storedParams: Record<string, unknown>
  /** One change, auto-saved: `null` clears the key back to its default. */
  setParam: (name: string, value: unknown) => void
  /** A whole set from an html page (`cinna/params-changed`, already merged). */
  setParams: (params: Record<string, unknown>) => void
  paramsRefusal: ParamsRefusal | null

  refresh: (reason: RefreshReason) => void
  /** A refresh or params change is running. */
  isRefreshing: boolean
  /** Why the last run did not happen (env asleep, call failed), one sentence. */
  runIssue: string | null

  callEndpoint: (
    name: string,
    args: Record<string, unknown>,
  ) => Promise<unknown>
  mintFrame: () => Promise<DashboardFramePublic>
  /**
   * The page's readiness for the agent's highlights. `page_doc: null` withdraws
   * it (the frame went away); `keepalive` lets that last write outlive the tab.
   */
  setPageState: (
    state: {
      page_doc: string | null
      highlight_ready: boolean
      /** Apply only while the stored doc is this one (another tab may own it now). */
      if_page_doc?: string
    },
    options?: { keepalive?: boolean },
  ) => void
}
