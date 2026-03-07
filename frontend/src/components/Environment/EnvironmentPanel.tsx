import { useState, useEffect, useCallback } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { Tabs } from "@/components/ui/tabs"
import { WorkspaceService, OpenAPI, AgentsService, CredentialsService, EnvironmentsService, WebappService } from "@/client"
import type { AxiosRequestConfig } from "axios"
import { TabHeader } from "./TabHeader"
import { WorkspaceTabContent } from "./WorkspaceTabContent"
import { CredentialsTabContent } from "./CredentialsTabContent"
import { LoadingState, ErrorState, NoEnvironmentState } from "./StateComponents"
import { MessageCircle, Wrench } from "lucide-react"
import { convertFileNodeToTreeItem, type FileNode } from "./utils"
import type { TreeItem, DatabaseTableItem } from "./types"
import useWorkspace from "@/hooks/useWorkspace"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { useGuestShare } from "@/hooks/useGuestShare"

interface WorkspaceTreeResponse {
  files?: FileNode
  scripts?: FileNode
  logs?: FileNode
  docs?: FileNode
  uploads?: FileNode
  webapp?: FileNode | null
}

// Default SDK identifier
const DEFAULT_SDK = "claude-code/anthropic"

interface EnvironmentPanelProps {
  isOpen: boolean
  environmentId?: string
  agentId?: string
}

export function EnvironmentPanel({ isOpen, environmentId, agentId }: EnvironmentPanelProps) {
  const [activeTab, setActiveTab] = useState("files")
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set())
  const [isWidePanelMode, setIsWidePanelMode] = useState(false)
  // Database tables state: path -> { tables: DatabaseTableItem[], loading: boolean, error: string | null }
  const [databaseTables, setDatabaseTables] = useState<Record<string, { tables: DatabaseTableItem[], loading: boolean, error: string | null }>>({})

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { activeWorkspaceId } = useWorkspace()
  const { isGuest } = useGuestShare()

  // Fetch agent credentials
  const {
    data: agentCredentialsData,
    isLoading: isLoadingAgentCredentials,
  } = useQuery({
    queryKey: ["agent-credentials", agentId],
    queryFn: () => AgentsService.readAgentCredentials({ id: agentId! }),
    enabled: !isGuest && !!agentId && activeTab === "credentials",
  })

  // Fetch all user credentials
  const {
    data: allCredentialsData,
    isLoading: isLoadingAllCredentials,
  } = useQuery({
    queryKey: ["credentials", activeWorkspaceId],
    queryFn: () => CredentialsService.readCredentials({
      skip: 0,
      limit: 100,
      userWorkspaceId: activeWorkspaceId ?? "",
    }),
    enabled: !isGuest && activeTab === "credentials",
  })

  const agentCredentials = agentCredentialsData?.data || []
  const allCredentials = allCredentialsData?.data || []

  // Fetch environment details to get SDK info
  const { data: environmentData } = useQuery({
    queryKey: ["environment", environmentId],
    queryFn: () => EnvironmentsService.getEnvironment({ id: environmentId! }),
    enabled: !isGuest && isOpen && !!environmentId,
  })

  // Fetch webapp status (check if webapp exists and is enabled)
  const { data: webappStatus } = useQuery({
    queryKey: ["webapp-status", agentId],
    queryFn: () => WebappService.getWebappStatus({ agentId: agentId! }) as Promise<{
      exists: boolean
      total_size_bytes: number
      file_count: number
      has_index: boolean
      api_endpoints: string[]
      webapp_enabled: boolean
    }>,
    enabled: !isGuest && isOpen && !!agentId && !!environmentId,
    staleTime: 30000,
  })

  // Compute webapp preview URL (only if enabled and has index.html)
  const webappPreviewUrl = webappStatus?.webapp_enabled && webappStatus?.has_index
    ? `${OpenAPI.BASE}/api/v1/agents/${agentId}/webapp/`
    : null

  // Check if a credential is shared with the agent
  const isCredentialShared = useCallback((credentialId: string) => {
    return agentCredentials.some((ac) => ac.id === credentialId)
  }, [agentCredentials])

  // Add credential mutation
  const addCredentialMutation = useMutation({
    mutationFn: (credentialId: string) =>
      AgentsService.addCredentialToAgent({
        id: agentId!,
        requestBody: { credential_id: credentialId },
      }),
    onSuccess: () => {
      showSuccessToast("Credential shared")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-credentials", agentId] })
    },
  })

  // Remove credential mutation
  const removeCredentialMutation = useMutation({
    mutationFn: (credentialId: string) =>
      AgentsService.removeCredentialFromAgent({
        id: agentId!,
        credentialId: credentialId,
      }),
    onSuccess: () => {
      showSuccessToast("Credential unshared")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-credentials", agentId] })
    },
  })

  // Toggle credential share/unshare
  const handleToggleCredential = useCallback((credentialId: string) => {
    if (isCredentialShared(credentialId)) {
      removeCredentialMutation.mutate(credentialId)
    } else {
      addCredentialMutation.mutate(credentialId)
    }
  }, [isCredentialShared, addCredentialMutation, removeCredentialMutation])

  // Set up request interceptor for blob downloads (only once)
  useEffect(() => {
    const interceptor = (config: AxiosRequestConfig) => {
      // If this is a download request, set responseType to blob
      if (config.url?.includes('/workspace/download/')) {
        config.responseType = 'blob'
      }
      return config
    }

    // Register interceptor
    OpenAPI.interceptors.request.use(interceptor)

    // Cleanup: remove interceptor when component unmounts
    return () => {
      OpenAPI.interceptors.request.eject(interceptor)
    }
  }, [])

  // Fetch workspace tree when panel is open and environmentId is available
  const {
    data: workspaceData,
    isLoading,
    error,
    refetch
  } = useQuery<WorkspaceTreeResponse>({
    queryKey: ["workspace-tree", environmentId],
    queryFn: async () => {
      const response = await WorkspaceService.getWorkspaceTree({ envId: environmentId! })
      return response as WorkspaceTreeResponse
    },
    enabled: isOpen && !!environmentId,
    staleTime: 5000, // Cache for 5 seconds
  })

  // Fetch database tables for a SQLite file (must be before conditional return)
  const handleFetchDatabaseTables = useCallback(async (dbPath: string) => {
    if (!environmentId) return

    // Check if already loading to avoid concurrent fetches
    const currentState = databaseTables[dbPath]
    if (currentState?.loading) return

    // Set loading state (always re-fetch to get fresh data on expand)
    setDatabaseTables(prev => ({
      ...prev,
      [dbPath]: { tables: prev[dbPath]?.tables || [], loading: true, error: null }
    }))

    try {
      const tableEntries = await WorkspaceService.getDatabaseTables({
        envId: environmentId,
        path: dbPath
      })

      // Convert API response to DatabaseTableItem objects
      const tables: DatabaseTableItem[] = (tableEntries as Array<{ name: string; type: string }>).map((entry) => ({
        type: "database_table" as const,
        name: entry.name,
        tableType: entry.type as "table" | "view",
        databasePath: dbPath
      }))

      setDatabaseTables(prev => ({
        ...prev,
        [dbPath]: { tables, loading: false, error: null }
      }))
    } catch (error) {
      setDatabaseTables(prev => ({
        ...prev,
        [dbPath]: { tables: [], loading: false, error: String(error) }
      }))
    }
  }, [environmentId, databaseTables])

  if (!isOpen) return null

  const handleDownload = async (filePath: string) => {
    if (!environmentId) return

    try {
      // Use generated WorkspaceService client
      // The interceptor will set responseType: 'blob' for this request
      const blob = await WorkspaceService.downloadWorkspaceItem({
        envId: environmentId,
        path: filePath
      }) as unknown as Blob

      // Extract filename from path
      const filename = filePath.split('/').pop() || 'download'

      // Create blob URL and trigger download
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      window.URL.revokeObjectURL(url)
    } catch (error) {
      console.error('Download error:', error)
    }
  }

  const handleToggleFolder = (path: string) => {
    setExpandedFolders((prev) => {
      const next = new Set(prev)
      if (next.has(path)) {
        next.delete(path)
      } else {
        next.add(path)
      }
      return next
    })
  }

  // Convert API data to TreeItem[] for each section
  const filesData: TreeItem[] = workspaceData?.files ? [convertFileNodeToTreeItem(workspaceData.files)] : []
  const scriptsData: TreeItem[] = workspaceData?.scripts ? [convertFileNodeToTreeItem(workspaceData.scripts)] : []
  const logsData: TreeItem[] = workspaceData?.logs ? [convertFileNodeToTreeItem(workspaceData.logs)] : []
  const docsData: TreeItem[] = workspaceData?.docs ? [convertFileNodeToTreeItem(workspaceData.docs)] : []
  const uploadsData: TreeItem[] = workspaceData?.uploads ? [convertFileNodeToTreeItem(workspaceData.uploads)] : []
  const webappData: TreeItem[] = workspaceData?.webapp ? [convertFileNodeToTreeItem(workspaceData.webapp)] : []

  return (
    <div className={`absolute top-0 right-0 h-full bg-background border-l border-border shadow-lg z-10 flex flex-col transition-all duration-200 ${isWidePanelMode ? 'w-[768px]' : 'w-96'}`}>
      <Tabs value={activeTab} onValueChange={setActiveTab} className="flex flex-col flex-1 min-h-0">
        <TabHeader
          activeTab={activeTab}
          onTabChange={setActiveTab}
          isWidePanelMode={isWidePanelMode}
          onToggleWidePanel={() => setIsWidePanelMode(!isWidePanelMode)}
          hideCredentials={isGuest}
          webappUrl={webappPreviewUrl}
          hasWebapp={!!workspaceData?.webapp}
        />

        {/* Credentials tab - separate from workspace tabs */}
        {activeTab === "credentials" ? (
          <CredentialsTabContent
            credentials={allCredentials}
            isLoading={isLoadingAgentCredentials || isLoadingAllCredentials}
            isCredentialShared={isCredentialShared}
            onToggleCredential={handleToggleCredential}
            isMutating={addCredentialMutation.isPending || removeCredentialMutation.isPending}
            mutatingCredentialId={
              addCredentialMutation.isPending ? addCredentialMutation.variables :
              removeCredentialMutation.isPending ? removeCredentialMutation.variables : undefined
            }
          />
        ) : (
          /* Show loading/error/no-env state for workspace tabs */
          !environmentId ? (
            <div className="flex-1"><NoEnvironmentState /></div>
          ) : isLoading ? (
            <div className="flex-1"><LoadingState /></div>
          ) : error ? (
            <div className="flex-1"><ErrorState error={error} onRetry={refetch} /></div>
          ) : (
            <>
              <WorkspaceTabContent
                value="files"
                data={filesData}
                expandedFolders={expandedFolders}
                onToggleFolder={handleToggleFolder}
                onDownload={handleDownload}
                pathPrefix="files"
                envId={environmentId}
                databaseTables={databaseTables}
                onFetchDatabaseTables={handleFetchDatabaseTables}
                isGuest={isGuest}
              />
              <WorkspaceTabContent
                value="scripts"
                data={scriptsData}
                expandedFolders={expandedFolders}
                onToggleFolder={handleToggleFolder}
                onDownload={handleDownload}
                pathPrefix="scripts"
                envId={environmentId}
                databaseTables={databaseTables}
                onFetchDatabaseTables={handleFetchDatabaseTables}
                isGuest={isGuest}
              />
              <WorkspaceTabContent
                value="logs"
                data={logsData}
                expandedFolders={expandedFolders}
                onToggleFolder={handleToggleFolder}
                onDownload={handleDownload}
                pathPrefix="logs"
                envId={environmentId}
                databaseTables={databaseTables}
                onFetchDatabaseTables={handleFetchDatabaseTables}
                isGuest={isGuest}
              />
              <WorkspaceTabContent
                value="docs"
                data={docsData}
                expandedFolders={expandedFolders}
                onToggleFolder={handleToggleFolder}
                onDownload={handleDownload}
                pathPrefix="docs"
                envId={environmentId}
                databaseTables={databaseTables}
                onFetchDatabaseTables={handleFetchDatabaseTables}
                isGuest={isGuest}
              />
              <WorkspaceTabContent
                value="uploads"
                data={uploadsData}
                expandedFolders={expandedFolders}
                onToggleFolder={handleToggleFolder}
                onDownload={handleDownload}
                pathPrefix="uploads"
                envId={environmentId}
                databaseTables={databaseTables}
                onFetchDatabaseTables={handleFetchDatabaseTables}
                isGuest={isGuest}
              />
              {webappData.length > 0 && (
                <WorkspaceTabContent
                  value="webapp"
                  data={webappData}
                  expandedFolders={expandedFolders}
                  onToggleFolder={handleToggleFolder}
                  onDownload={handleDownload}
                  pathPrefix="webapp"
                  envId={environmentId}
                  databaseTables={databaseTables}
                  onFetchDatabaseTables={handleFetchDatabaseTables}
                  isGuest={isGuest}
                />
              )}
            </>
          )
        )}
      </Tabs>

      {/* SDK Status Footer */}
      {environmentData && (
        <div className="px-3 py-2 border-t border-border text-xs text-muted-foreground opacity-30 flex items-center gap-3">
          <div className="flex items-center gap-1">
            <MessageCircle className="h-3 w-3" />
            <span>{environmentData.agent_sdk_conversation || DEFAULT_SDK}</span>
          </div>
          <div className="flex items-center gap-1">
            <Wrench className="h-3 w-3" />
            <span>{environmentData.agent_sdk_building || DEFAULT_SDK}</span>
          </div>
        </div>
      )}
    </div>
  )
}
