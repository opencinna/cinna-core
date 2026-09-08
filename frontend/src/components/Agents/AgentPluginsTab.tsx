import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Search,
  Store,
  XCircle,
  Zap,
} from "lucide-react"
import { useEffect, useState } from "react"

import type {
  LLMPluginMarketplacePluginPublic,
  PluginSyncResponse,
} from "@/client"
import { LlmPluginsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { EventTypes, eventService } from "@/services/eventService"
import { handleError } from "@/utils"
import { InstalledPluginsCard } from "./InstalledPluginsCard"
import { InstallPluginModal } from "./InstallPluginModal"
import { PluginCard } from "./PluginCard"

/** One plugin failure carried by a PLUGIN_SYNC_WARNING event. */
interface PluginSyncFailure {
  marketplace_name: string
  plugin_name: string
  source?: string
  error_message?: string | null
}

/** Aggregated live plugin-sync warning surfaced as an amber banner. */
interface PluginSyncWarning {
  environmentId: string
  instanceName: string
  failures: PluginSyncFailure[]
}

interface AgentPluginsTabProps {
  agentId: string
}

export function AgentPluginsTab({ agentId }: AgentPluginsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isInstallDialogOpen, setIsInstallDialogOpen] = useState(false)
  const [selectedPlugin, setSelectedPlugin] =
    useState<LLMPluginMarketplacePluginPublic | null>(null)
  const [searchQuery, setSearchQuery] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")
  const [currentPage, setCurrentPage] = useState(1)
  const PLUGINS_PER_PAGE = 30

  // Sync progress dialog state
  const [syncProgress, setSyncProgress] = useState<{
    isOpen: boolean
    title: string
    syncResult: PluginSyncResponse | null
  }>({
    isOpen: false,
    title: "",
    syncResult: null,
  })

  // Live plugin-sync warnings, keyed by environment id (latest wins per env).
  const [syncWarnings, setSyncWarnings] = useState<
    Record<string, PluginSyncWarning>
  >({})

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchQuery)
      setCurrentPage(1) // Reset to page 1 on search change
    }, 300)
    return () => clearTimeout(timer)
  }, [searchQuery])

  // Subscribe to PLUGIN_SYNC_WARNING: show a live amber banner when a plugin
  // fails to install on env start/rebuild, and refresh the installed list
  // (mirrors the model-health / agent-api live-event pattern).
  useEffect(() => {
    if (!agentId) return
    const subId = eventService.subscribe(
      EventTypes.PLUGIN_SYNC_WARNING,
      (event) => {
        if (event.model_id && event.model_id !== agentId) return
        const meta = event.meta || {}
        const environmentId = String(meta.environment_id || "")
        const failures = Array.isArray(meta.failures)
          ? (meta.failures as PluginSyncFailure[])
          : []
        if (!environmentId || failures.length === 0) return
        setSyncWarnings((prev) => ({
          ...prev,
          [environmentId]: {
            environmentId,
            instanceName: String(meta.instance_name || environmentId),
            failures,
          },
        }))
        queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })
      },
    )
    return () => eventService.unsubscribe(subId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, queryClient])

  // Fetch installed plugins for this agent
  const {
    data: installedPluginsData,
    isLoading: isLoadingInstalled,
    isError: isInstalledError,
    error: installedError,
    refetch: refetchInstalled,
  } = useQuery({
    queryKey: ["agent-plugins", agentId],
    queryFn: () => LlmPluginsService.listAgentPlugins({ agentId }),
    enabled: !!agentId,
  })

  const installedPlugins = installedPluginsData?.data || []
  const installedPluginIds = new Set(installedPlugins.map((p) => p.plugin_id))

  // Fetch available plugins for discovery with backend search and pagination
  const { data: availablePluginsData, isLoading: isLoadingAvailable } =
    useQuery({
      queryKey: ["discover-plugins", debouncedSearch, currentPage],
      queryFn: () =>
        LlmPluginsService.discoverPlugins({
          search: debouncedSearch || undefined,
          skip: (currentPage - 1) * PLUGINS_PER_PAGE,
          limit: PLUGINS_PER_PAGE,
        }),
    })

  const availablePlugins = availablePluginsData?.data || []
  const totalAvailableCount = availablePluginsData?.count || 0

  // Filter out already installed plugins from available list (client-side since installed list is separate)
  const notInstalledPlugins = availablePlugins.filter(
    (p) => !installedPluginIds.has(p.id),
  )

  // This card's whole list is a *subtraction*, so it is only correct while the
  // installed set is known. In flight, `installedPluginIds` is empty and the
  // filter removes nothing; failed, it stays empty for good. Either way the
  // grid would offer Install on plugins this agent already has. The tab used
  // to make that structurally impossible with an early return that blanked
  // everything; now that the installed list renders its own states in its own
  // card, the subtraction's premise is gated here instead.
  const installSetUnknown = isLoadingInstalled || !installedPluginsData

  // Calculate pagination based on backend total count
  const totalPages = Math.ceil(totalAvailableCount / PLUGINS_PER_PAGE)

  // Install mutation
  const installMutation = useMutation({
    mutationFn: ({
      pluginId,
      conversationMode,
      buildingMode,
    }: {
      pluginId: string
      conversationMode: boolean
      buildingMode: boolean
    }) =>
      LlmPluginsService.installAgentPlugin({
        agentId,
        requestBody: {
          plugin_id: pluginId,
          conversation_mode: conversationMode,
          building_mode: buildingMode,
        },
      }),
    onSuccess: (data) => {
      setIsInstallDialogOpen(false)
      setSelectedPlugin(null)
      // Show the dialog on env-level failures OR per-plugin install failures.
      if (
        (data.failed_syncs && data.failed_syncs > 0) ||
        data.partial_failures
      ) {
        setSyncProgress({
          isOpen: true,
          title: "Plugin Installed",
          syncResult: data,
        })
      } else {
        showSuccessToast("Plugin installed successfully")
      }
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })
    },
  })

  const handleInstall = (conversationMode: boolean, buildingMode: boolean) => {
    if (selectedPlugin) {
      installMutation.mutate({
        pluginId: selectedPlugin.id,
        conversationMode,
        buildingMode,
      })
    }
  }

  /**
   * The one thing the rows cannot own: the dialog that lists a partial sync
   * failure. A row keeps its own pending state (a shared `useMutation`
   * describes only its latest call), and hands the failure report up here.
   */
  const reportSyncResult = (title: string, result: PluginSyncResponse) => {
    setSyncProgress({ isOpen: true, title, syncResult: result })
  }

  const openInstallDialog = (plugin: LLMPluginMarketplacePluginPublic) => {
    setSelectedPlugin(plugin)
    setIsInstallDialogOpen(true)
  }

  const activeWarnings = Object.values(syncWarnings)

  return (
    <div className="space-y-6">
      {/* Plugin sync warning banner — live, non-blocking. One row per env that
          reported a plugin install failure on start/rebuild. */}
      {activeWarnings.length > 0 && (
        <div className="space-y-2">
          {activeWarnings.map((warning) => (
            <div
              key={warning.environmentId}
              className="flex items-start gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200"
            >
              <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" />
              <div className="flex-1 space-y-1">
                <p className="font-medium">
                  Some plugins failed to install in{" "}
                  <span className="font-semibold">{warning.instanceName}</span>.
                  The environment started without them.
                </p>
                <ul className="list-inside list-disc space-y-0.5">
                  {warning.failures.map((f, i) => (
                    <li key={`${f.marketplace_name}/${f.plugin_name}-${i}`}>
                      <span className="font-medium">
                        {f.marketplace_name}/{f.plugin_name}
                      </span>
                      {f.error_message ? `: ${f.error_message}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-amber-700 hover:text-amber-900 dark:text-amber-300"
                onClick={() =>
                  setSyncWarnings((prev) => {
                    const next = { ...prev }
                    delete next[warning.environmentId]
                    return next
                  })
                }
              >
                Dismiss
              </Button>
            </div>
          ))}
        </div>
      )}

      {/* Installed plugins — the A10 rebuild: house rows on `PreviewList`,
          one segmented mode toggle per row, everything else in the `⋯`. */}
      <InstalledPluginsCard
        agentId={agentId}
        plugins={installedPlugins}
        isLoading={isLoadingInstalled}
        // Gated on there being nothing to show, so a failed background refetch
        // keeps the rows on screen; a first failed load still renders as a
        // failure, never as "no plugins installed yet".
        isError={isInstalledError && !installedPluginsData}
        error={installedError}
        onRetry={() => refetchInstalled()}
        onSyncResult={reportSyncResult}
      />

      {/* Discover Plugins Section */}
      <Card>
        <CardHeader>
          {/* A9 fix-on-touch: the header icon its rebuilt sibling above now
              has, so the two cards on this tab read as one treatment. */}
          <CardTitle className="flex items-center gap-2 min-w-0">
            <Store className="h-5 w-5 shrink-0" />
            Available plugins
          </CardTitle>
          <CardDescription>
            Discover and install plugins from marketplaces.
          </CardDescription>
          <div className="relative mt-3">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Search by name, description, author, or category..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-9"
            />
          </div>
        </CardHeader>
        <CardContent>
          {/* Branch order matters: the error case is also an
              `installSetUnknown` case, so it has to be tested first or the
              skeletons would spin forever on a failed installed-list read. */}
          {isInstalledError && !installedPluginsData ? (
            // The installed list failed, so this list cannot be trusted to
            // exclude what is already installed. Say that instead of showing a
            // grid of Install buttons that may re-install.
            <QueryErrorAlert
              error={installedError}
              fallback="Couldn't check which plugins are already installed"
              onRetry={() => refetchInstalled()}
            >
              <p className="text-xs">
                Available plugins stay hidden until that succeeds, so nothing
                already installed is offered again.
              </p>
            </QueryErrorAlert>
          ) : isLoadingAvailable || installSetUnknown ? (
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              <Skeleton className="h-32 w-full" />
              <Skeleton className="h-32 w-full" />
              <Skeleton className="h-32 w-full" />
            </div>
          ) : notInstalledPlugins.length === 0 && !debouncedSearch ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <p className="text-muted-foreground mb-2">
                {totalAvailableCount === 0
                  ? "No plugins available yet."
                  : "All available plugins are already installed."}
              </p>
              {totalAvailableCount === 0 && (
                <p className="text-sm text-muted-foreground">
                  Ask an admin to add a plugin marketplace.
                </p>
              )}
            </div>
          ) : notInstalledPlugins.length === 0 && debouncedSearch ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <p className="text-muted-foreground mb-2">
                No plugins match your search.
              </p>
              <Button variant="ghost" onClick={() => setSearchQuery("")}>
                Clear search
              </Button>
            </div>
          ) : (
            <>
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {notInstalledPlugins.map((plugin) => (
                  <PluginCard
                    key={plugin.id}
                    plugin={plugin}
                    onInstall={() => openInstallDialog(plugin)}
                  />
                ))}
              </div>
              {totalPages > 1 && (
                <div className="flex items-center justify-between mt-6 pt-4 border-t">
                  <p className="text-sm text-muted-foreground">
                    Showing {(currentPage - 1) * PLUGINS_PER_PAGE + 1}-
                    {Math.min(
                      currentPage * PLUGINS_PER_PAGE,
                      totalAvailableCount,
                    )}{" "}
                    of {totalAvailableCount} plugins
                  </p>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                      disabled={currentPage === 1}
                    >
                      <ChevronLeft className="h-4 w-4 mr-1" />
                      Previous
                    </Button>
                    <span className="text-sm text-muted-foreground px-2">
                      Page {currentPage} of {totalPages}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        setCurrentPage((p) => Math.min(totalPages, p + 1))
                      }
                      disabled={currentPage === totalPages}
                    >
                      Next
                      <ChevronRight className="h-4 w-4 ml-1" />
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Install Plugin Modal */}
      <InstallPluginModal
        open={isInstallDialogOpen}
        onOpenChange={setIsInstallDialogOpen}
        plugin={selectedPlugin}
        onInstall={handleInstall}
        isLoading={installMutation.isPending}
      />

      {/* Sync Error Dialog - only shown when there are sync failures */}
      <Dialog
        open={syncProgress.isOpen}
        onOpenChange={(open) => {
          if (!open) {
            setSyncProgress({ isOpen: false, title: "", syncResult: null })
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-amber-500" />
              {syncProgress.title} - Sync Issues
            </DialogTitle>
            <DialogDescription>
              {syncProgress.syncResult?.message}
            </DialogDescription>
          </DialogHeader>
          {syncProgress.syncResult && (
            <div className="space-y-3 mt-2">
              {syncProgress.syncResult.environments_synced &&
                syncProgress.syncResult.environments_synced.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-sm font-medium">Environment Status:</p>
                    <div className="space-y-1 max-h-60 overflow-y-auto">
                      {syncProgress.syncResult.environments_synced?.map(
                        (env) => (
                          <div
                            key={env.environment_id}
                            className="flex items-center justify-between p-2 rounded-md bg-muted text-sm"
                          >
                            <div className="flex items-center gap-2">
                              {env.status === "success" ||
                              env.status === "activated_and_synced" ? (
                                <CheckCircle2 className="h-4 w-4 text-green-500" />
                              ) : (
                                <XCircle className="h-4 w-4 text-red-500" />
                              )}
                              <span>{env.instance_name}</span>
                              {env.was_suspended && (
                                <Badge variant="outline" className="text-xs">
                                  <Zap className="h-3 w-3 mr-1" />
                                  Activated
                                </Badge>
                              )}
                            </div>
                            {env.error_message && (
                              <span
                                className="text-xs text-red-500 max-w-[200px] truncate"
                                title={env.error_message}
                              >
                                {env.error_message}
                              </span>
                            )}
                          </div>
                        ),
                      )}
                    </div>
                  </div>
                )}
              {/* Per-plugin install failures (env reached, but a plugin could
                  not be fetched/seeded — excluded from the active set). */}
              {syncProgress.syncResult.plugin_results &&
                syncProgress.syncResult.plugin_results.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-sm font-medium">Plugin Failures:</p>
                    <div className="space-y-1 max-h-60 overflow-y-auto">
                      {syncProgress.syncResult.plugin_results.map((p, i) => (
                        <div
                          key={`${p.marketplace_name}/${p.plugin_name}-${i}`}
                          className="flex items-center justify-between p-2 rounded-md bg-muted text-sm"
                        >
                          <div className="flex items-center gap-2">
                            <XCircle className="h-4 w-4 text-red-500" />
                            <span>
                              {p.marketplace_name}/{p.plugin_name}
                            </span>
                          </div>
                          {p.error_message && (
                            <span
                              className="text-xs text-red-500 max-w-[200px] truncate"
                              title={p.error_message}
                            >
                              {p.error_message}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              <div className="flex justify-end">
                <Button
                  onClick={() =>
                    setSyncProgress({
                      isOpen: false,
                      title: "",
                      syncResult: null,
                    })
                  }
                >
                  Close
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
