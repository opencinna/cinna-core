import { useState, useEffect } from "react"
import { useQuery } from "@tanstack/react-query"
import { Tabs } from "@/components/ui/tabs"
import { WorkspaceService, OpenAPI } from "@/client"
import type { AxiosRequestConfig } from "axios"
import { TabHeader } from "./TabHeader"
import { WorkspaceTabContent } from "./WorkspaceTabContent"
import { LoadingState, ErrorState, NoEnvironmentState } from "./StateComponents"
import { convertFileNodeToTreeItem, type FileNode } from "./utils"
import type { TreeItem } from "./types"

interface WorkspaceTreeResponse {
  files?: FileNode
  scripts?: FileNode
  logs?: FileNode
  docs?: FileNode
  uploads?: FileNode
}

interface EnvironmentPanelProps {
  isOpen: boolean
  environmentId?: string
}

export function EnvironmentPanel({ isOpen, environmentId }: EnvironmentPanelProps) {
  const [activeTab, setActiveTab] = useState("files")
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set())
  const [isWidePanelMode, setIsWidePanelMode] = useState(false)

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

  return (
    <div className={`absolute top-0 right-0 h-full bg-background border-l border-border shadow-lg z-10 flex flex-col transition-all duration-200 ${isWidePanelMode ? 'w-[768px]' : 'w-96'}`}>
      <Tabs value={activeTab} onValueChange={setActiveTab} className="flex flex-col flex-1 min-h-0">
        <TabHeader
          activeTab={activeTab}
          onTabChange={setActiveTab}
          isWidePanelMode={isWidePanelMode}
          onToggleWidePanel={() => setIsWidePanelMode(!isWidePanelMode)}
        />

        {/* Show loading/error/no-env state for all tabs */}
        {!environmentId ? (
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
            />
            <WorkspaceTabContent
              value="scripts"
              data={scriptsData}
              expandedFolders={expandedFolders}
              onToggleFolder={handleToggleFolder}
              onDownload={handleDownload}
              pathPrefix="scripts"
              envId={environmentId}
            />
            <WorkspaceTabContent
              value="logs"
              data={logsData}
              expandedFolders={expandedFolders}
              onToggleFolder={handleToggleFolder}
              onDownload={handleDownload}
              pathPrefix="logs"
              envId={environmentId}
            />
            <WorkspaceTabContent
              value="docs"
              data={docsData}
              expandedFolders={expandedFolders}
              onToggleFolder={handleToggleFolder}
              onDownload={handleDownload}
              pathPrefix="docs"
              envId={environmentId}
            />
            <WorkspaceTabContent
              value="uploads"
              data={uploadsData}
              expandedFolders={expandedFolders}
              onToggleFolder={handleToggleFolder}
              onDownload={handleDownload}
              pathPrefix="uploads"
              envId={environmentId}
            />
          </>
        )}
      </Tabs>
    </div>
  )
}
