import { createFileRoute, redirect } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { RowSelectionState } from "@tanstack/react-table"

import { AdminEnvironmentsService } from "@/client"
import { usePageHeader } from "@/routes/_layout"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { eventService, EventTypes } from "@/services/eventService"
import { APP_NAME } from "@/utils"
import { AdminEnvFiltersBar, type AdminEnvFilters } from "@/components/Admin/Environments/AdminEnvFiltersBar"
import { AdminEnvStaleBanner } from "@/components/Admin/Environments/AdminEnvStaleBanner"
import { AdminEnvTable, TRANSITIONAL_STATUSES } from "@/components/Admin/Environments/AdminEnvTable"
import PendingItems from "@/components/Pending/PendingItems"

// ---------------------------------------------------------------------------
// Route definition
// ---------------------------------------------------------------------------

export const Route = createFileRoute("/_layout/admin/agent-envs")({
  component: AdminAgentEnvs,
  head: () => ({
    meta: [
      {
        title: `Agent Environments - Admin - ${APP_NAME}`,
      },
    ],
  }),
  beforeLoad: async ({ context }) => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    // context.user is available after the _layout auth guard
    const user = (context as any)?.user
    if (user && !user.is_superuser) {
      throw redirect({ to: "/" })
    }
  },
})

// ---------------------------------------------------------------------------
// Query helpers
// ---------------------------------------------------------------------------

function buildQueryKey(filters: AdminEnvFilters) {
  return ["admin", "agent-environments", filters] as const
}

function buildQueryFn(filters: AdminEnvFilters) {
  return () =>
    AdminEnvironmentsService.listAdminEnvironments({
      template: filters.template ?? undefined,
      status: filters.status ?? undefined,
      isStale: filters.isStale ?? undefined,
      inUse: filters.inUse ?? undefined,
      search: filters.search || undefined,
    })
}

// ---------------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------------

function AdminAgentEnvs() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [filters, setFilters] = useState<AdminEnvFilters>({
    template: null,
    status: null,
    isStale: null,
    inUse: null,
    search: "",
  })

  // Selection state is lifted here so the stale banner's "Select all stale"
  // button can programmatically select stale rows without touching filters
  // (which would trigger a refetch and hide the banner).
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})

  // ── Main data query ──────────────────────────────────────────────────────
  const { data, isLoading, isError } = useQuery({
    queryKey: buildQueryKey(filters),
    queryFn: buildQueryFn(filters),
    staleTime: 30_000,
    refetchInterval: 60_000,
  })

  // ── Bulk rebuild mutation ────────────────────────────────────────────────
  const bulkRebuildMutation = useMutation({
    mutationFn: (envIds: string[]) =>
      AdminEnvironmentsService.bulkRebuildEnvironments({
        requestBody: { environment_ids: envIds },
      }),
    onSuccess: (result) => {
      const queued = result.queued_environment_ids.length
      const skipped = result.skipped.length
      const parts: string[] = []
      if (queued > 0) parts.push(`Rebuild queued for ${queued} environment${queued !== 1 ? "s" : ""}`)
      if (skipped > 0) parts.push(`${skipped} skipped`)
      showSuccessToast(parts.join(". "))
      setRowSelection({})
      void queryClient.invalidateQueries({ queryKey: ["admin", "agent-environments"] })
    },
    onError: () => {
      showErrorToast("Failed to queue rebuild. Please try again.")
    },
  })

  // ── WebSocket: invalidate on environment status changes ──────────────────
  useEffect(() => {
    const subId = eventService.subscribe(
      EventTypes.ENVIRONMENT_STATUS_CHANGED,
      () => {
        void queryClient.invalidateQueries({ queryKey: ["admin", "agent-environments"] })
      }
    )
    return () => {
      eventService.unsubscribe(subId)
    }
  }, [queryClient])

  // ── Page header ──────────────────────────────────────────────────────────
  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Agent Environments</h1>
        <p className="text-xs text-muted-foreground">
          Rebuild environments after system updates
        </p>
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // Guard: non-superuser who made it past beforeLoad (race condition edge case)
  if (user && !user.is_superuser) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        You do not have permission to view this page.
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="p-6 md:p-8 overflow-y-auto">
        <div className="mx-auto max-w-7xl">
          <PendingItems />
        </div>
      </div>
    )
  }

  if (isError || !data) {
    return (
      <div className="p-6 md:p-8 overflow-y-auto">
        <div className="mx-auto max-w-7xl">
          <div className="flex flex-col items-center justify-center gap-2 py-20 text-center">
            <p className="text-muted-foreground">
              Failed to load environments. Please try refreshing the page.
            </p>
          </div>
        </div>
      </div>
    )
  }

  const handleSelectAllStale = () => {
    // Build a RowSelectionState selecting every stale row that is not in a
    // transitional status (those rows have selection disabled in the table).
    // We intentionally do not touch filters.isStale — doing so would refetch
    // the list and hide the banner via the `!filters.isStale` guard below.
    const selection: RowSelectionState = {}
    for (const env of data.data) {
      if (env.is_stale && !TRANSITIONAL_STATUSES.has(env.status)) {
        selection[env.id] = true
      }
    }
    setRowSelection(selection)
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-4">
        {/* Stale banner — hidden when the list is already filtered to stale-only
            (otherwise the banner would read "42 of 42" and be noise) */}
        {!filters.isStale && (
          <AdminEnvStaleBanner
            staleCount={data.stale_count}
            totalCount={data.count}
            onSelectAllStale={handleSelectAllStale}
          />
        )}

        {/* Filters */}
        <AdminEnvFiltersBar
          data={data}
          filters={filters}
          onFiltersChange={setFilters}
        />

        {/* Summary counts */}
        <div className="flex items-center gap-4 text-xs text-muted-foreground">
          <span>
            <strong>{data.count}</strong> environments
          </span>
          {data.stale_count > 0 && (
            <span className="text-orange-600 dark:text-orange-400">
              <strong>{data.stale_count}</strong> stale
            </span>
          )}
          {data.in_use_count > 0 && (
            <span className="text-blue-600 dark:text-blue-400">
              <strong>{data.in_use_count}</strong> in use
            </span>
          )}
        </div>

        {/* Table */}
        <AdminEnvTable
          data={data.data}
          onRebuildSelected={async (ids) => { await bulkRebuildMutation.mutateAsync(ids) }}
          isRebuildPending={bulkRebuildMutation.isPending}
          rowSelection={rowSelection}
          onRowSelectionChange={setRowSelection}
        />
      </div>
    </div>
  )
}
