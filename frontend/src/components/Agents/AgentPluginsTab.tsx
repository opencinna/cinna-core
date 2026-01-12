import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowUpCircle,
  MessageCircle,
  Wrench,
  Tag,
  Search,
  ChevronLeft,
  ChevronRight,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Zap,
} from "lucide-react"
import { useState, useEffect } from "react"

import type {
  AgentPluginLinkWithUpdateInfo,
  LLMPluginMarketplacePluginPublic,
  PluginSyncResponse,
} from "@/client"
import { LlmPluginsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { InstallPluginModal } from "./InstallPluginModal"
import { PluginCard } from "./PluginCard"

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
    isLoading: boolean
    syncResult: PluginSyncResponse | null
  }>({
    isOpen: false,
    title: "",
    isLoading: false,
    syncResult: null,
  })

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchQuery)
      setCurrentPage(1) // Reset to page 1 on search change
    }, 300)
    return () => clearTimeout(timer)
  }, [searchQuery])

  // Fetch installed plugins for this agent
  const {
    data: installedPluginsData,
    isLoading: isLoadingInstalled,
    error: installedError,
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
    (p) => !installedPluginIds.has(p.id)
  )

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
      // Check if sync had any failures
      if (data.failed_syncs && data.failed_syncs > 0) {
        // Show dialog with error details
        setSyncProgress({
          isOpen: true,
          title: "Plugin Installed",
          isLoading: false,
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

  // Uninstall mutation
  const uninstallMutation = useMutation({
    mutationFn: (linkId: string) =>
      LlmPluginsService.uninstallAgentPlugin({ agentId, linkId }),
    onSuccess: () => {
      showSuccessToast("Plugin uninstalled successfully")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })
    },
  })

  // Update mode mutation
  const updateModeMutation = useMutation({
    mutationFn: ({
      linkId,
      conversationMode,
      buildingMode,
      disabled,
    }: {
      linkId: string
      conversationMode?: boolean | null
      buildingMode?: boolean | null
      disabled?: boolean | null
    }) =>
      LlmPluginsService.updateAgentPlugin({
        agentId,
        linkId,
        requestBody: {
          conversation_mode: conversationMode,
          building_mode: buildingMode,
          disabled: disabled,
        },
      }),
    onSuccess: (data, variables) => {
      // Check if sync had any failures
      if (data.failed_syncs && data.failed_syncs > 0) {
        // Show dialog with error details
        setSyncProgress({
          isOpen: true,
          title: variables.disabled !== undefined
            ? (variables.disabled ? "Plugin Disabled" : "Plugin Enabled")
            : "Plugin Updated",
          isLoading: false,
          syncResult: data,
        })
      } else {
        // Just show success toast
        if (variables.disabled !== undefined && variables.disabled !== null) {
          showSuccessToast(variables.disabled ? "Plugin disabled" : "Plugin enabled")
        } else {
          showSuccessToast("Plugin modes updated")
        }
      }
    },
    onError: (error: unknown) => {
      handleError.call(showErrorToast, error as Parameters<typeof handleError>[0])
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })
    },
  })

  // Upgrade mutation
  const upgradeMutation = useMutation({
    mutationFn: (linkId: string) =>
      LlmPluginsService.upgradeAgentPlugin({ agentId, linkId }),
    onSuccess: () => {
      showSuccessToast("Plugin upgraded to latest version")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })
    },
  })

  const handleInstall = (
    conversationMode: boolean,
    buildingMode: boolean
  ) => {
    if (selectedPlugin) {
      installMutation.mutate({
        pluginId: selectedPlugin.id,
        conversationMode,
        buildingMode,
      })
    }
  }

  const handleModeToggle = (
    plugin: AgentPluginLinkWithUpdateInfo,
    mode: "conversation" | "building"
  ) => {
    updateModeMutation.mutate({
      linkId: plugin.id,
      conversationMode:
        mode === "conversation" ? !plugin.conversation_mode : undefined,
      buildingMode:
        mode === "building" ? !plugin.building_mode : undefined,
    })
  }

  const handleDisableToggle = (plugin: AgentPluginLinkWithUpdateInfo) => {
    updateModeMutation.mutate({
      linkId: plugin.id,
      disabled: !plugin.disabled,
    })
  }

  const openInstallDialog = (plugin: LLMPluginMarketplacePluginPublic) => {
    setSelectedPlugin(plugin)
    setIsInstallDialogOpen(true)
  }

  if (isLoadingInstalled) {
    return (
      <Card>
        <CardContent className="pt-6">
          <div className="space-y-2">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        </CardContent>
      </Card>
    )
  }

  if (installedError) {
    return (
      <Card>
        <CardContent className="pt-6">
          <p className="text-destructive">
            Error loading plugins: {(installedError as Error).message}
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-6">
      {/* Installed Plugins Section */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Installed Plugins</CardTitle>
              <CardDescription>
                Plugins installed for this agent. Enable them for conversation
                or building mode.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {installedPlugins.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <p className="text-muted-foreground mb-4">
                No plugins installed for this agent yet.
              </p>
              <p className="text-sm text-muted-foreground">
                Browse available plugins below to install one.
              </p>
            </div>
          ) : (
            <Table className="table-fixed w-full">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[50px]"></TableHead>
                  <TableHead className="w-auto">Plugin</TableHead>
                  <TableHead className="text-center w-[70px]">
                    <div className="flex items-center justify-center gap-1">
                      <MessageCircle className="h-4 w-4" />
                      Chat
                    </div>
                  </TableHead>
                  <TableHead className="text-center w-[70px]">
                    <div className="flex items-center justify-center gap-1">
                      <Wrench className="h-4 w-4" />
                      Build
                    </div>
                  </TableHead>
                  <TableHead className="w-[120px]"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {installedPlugins.map((plugin) => (
                  <TableRow key={plugin.id} className={plugin.disabled ? "opacity-50" : ""}>
                    <TableCell className="text-center">
                      <Switch
                        checked={!plugin.disabled}
                        onCheckedChange={() => handleDisableToggle(plugin)}
                        disabled={updateModeMutation.isPending}
                        className="data-[state=checked]:bg-green-500"
                      />
                    </TableCell>
                    <TableCell className="overflow-hidden">
                      <div className="flex flex-col gap-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium">
                            {plugin.plugin_name || "Unknown Plugin"}
                          </span>
                          {plugin.installed_version && (
                            <Badge variant="secondary" className="text-xs">
                              v{plugin.installed_version}
                            </Badge>
                          )}
                          {plugin.has_update && !plugin.disabled && (
                            <Badge
                              variant="default"
                              className="text-xs bg-blue-500 hover:bg-blue-600"
                            >
                              Update to v{plugin.latest_version}
                            </Badge>
                          )}
                        </div>
                        {plugin.plugin_category && (
                          <Badge variant="outline" className="text-xs w-fit">
                            <Tag className="mr-1 h-3 w-3" />
                            {plugin.plugin_category}
                          </Badge>
                        )}
                        {plugin.plugin_description && (
                          <span className="text-xs text-muted-foreground break-words whitespace-normal">
                            {plugin.plugin_description}
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-center">
                      <Switch
                        checked={plugin.conversation_mode}
                        onCheckedChange={() =>
                          handleModeToggle(plugin, "conversation")
                        }
                        disabled={updateModeMutation.isPending || plugin.disabled}
                        className="data-[state=checked]:bg-blue-500"
                      />
                    </TableCell>
                    <TableCell className="text-center">
                      <Switch
                        checked={plugin.building_mode}
                        onCheckedChange={() =>
                          handleModeToggle(plugin, "building")
                        }
                        disabled={updateModeMutation.isPending || plugin.disabled}
                        className="data-[state=checked]:bg-orange-500"
                      />
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        {plugin.has_update && !plugin.disabled && (
                          <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => upgradeMutation.mutate(plugin.id)}
                            disabled={upgradeMutation.isPending}
                            title="Upgrade to latest version"
                          >
                            <ArrowUpCircle className="h-4 w-4 text-blue-500" />
                          </Button>
                        )}
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => uninstallMutation.mutate(plugin.id)}
                          disabled={uninstallMutation.isPending}
                          className="text-destructive hover:text-destructive"
                        >
                          Uninstall
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Discover Plugins Section */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Available Plugins</CardTitle>
              <CardDescription>
                Discover and install plugins from marketplaces.
              </CardDescription>
            </div>
          </div>
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
          {isLoadingAvailable ? (
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
                    {Math.min(currentPage * PLUGINS_PER_PAGE, totalAvailableCount)} of{" "}
                    {totalAvailableCount} plugins
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
                      onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
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
            setSyncProgress({ isOpen: false, title: "", isLoading: false, syncResult: null })
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
              {syncProgress.syncResult.environments_synced && syncProgress.syncResult.environments_synced.length > 0 && (
                <div className="space-y-2">
                  <p className="text-sm font-medium">Environment Status:</p>
                  <div className="space-y-1 max-h-60 overflow-y-auto">
                    {syncProgress.syncResult.environments_synced?.map((env) => (
                      <div
                        key={env.environment_id}
                        className="flex items-center justify-between p-2 rounded-md bg-muted text-sm"
                      >
                        <div className="flex items-center gap-2">
                          {env.status === "success" || env.status === "activated_and_synced" ? (
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
                          <span className="text-xs text-red-500 max-w-[200px] truncate" title={env.error_message}>
                            {env.error_message}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <div className="flex justify-end">
                <Button
                  onClick={() => setSyncProgress({ isOpen: false, title: "", isLoading: false, syncResult: null })}
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
