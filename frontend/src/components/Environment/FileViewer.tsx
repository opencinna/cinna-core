import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Download, Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { WorkspaceService, EnvironmentsService, OpenAPI } from "@/client"
import { saveBlobAs } from "@/utils"
import { CSVViewer } from "./CSVViewer"
import { MarkdownViewer } from "./MarkdownViewer"
import { JSONViewer } from "./JSONViewer"
import { TextViewer } from "./TextViewer"
import { useEffect, useRef } from "react"
import { usePageHeader } from "@/routes/_layout"
import { eventService, EventTypes } from "@/services/eventService"
import type { AxiosRequestConfig } from "axios"

interface FileViewerProps {
  envId: string
  filePath: string
}

export function FileViewer({ envId, filePath }: FileViewerProps) {
  const { setHeaderContent } = usePageHeader()
  const queryClient = useQueryClient()
  const usageIntentSent = useRef(false)

  // Extract filename from path
  const filename = filePath.split("/").pop() || "file"
  const fileExtension = filename.split(".").pop()?.toLowerCase()

  // Workspace endpoints require the env to be running. Watch the env status
  // and (if needed) wake a suspended env via agent_usage_intent so the file
  // viewer works on a fresh tab without first opening the session.
  const { data: environment } = useQuery({
    queryKey: ["environment", envId],
    queryFn: () => EnvironmentsService.getEnvironment({ id: envId }),
    enabled: !!envId,
  })
  const envStatus = environment?.status
  const isEnvRunning = envStatus === "running"

  useEffect(() => {
    if (!envId || usageIntentSent.current) return
    if (envStatus && isEnvRunning) return
    usageIntentSent.current = true
    // Use the REST endpoint so env wake-up works even when the WebSocket is
    // permanently disconnected (e.g. after a backend deploy).
    EnvironmentsService.registerEnvironmentUsageIntent({ id: envId }).catch((error) => {
      console.error("Failed to send agent usage intent:", error)
      // Reset so a later status refresh can retry the wake-up.
      usageIntentSent.current = false
    })
  }, [envId, envStatus, isEnvRunning])

  useEffect(() => {
    if (!envId) return
    const subs: string[] = []
    const refresh = (event: { model_id?: string }) => {
      if (event.model_id === envId) {
        queryClient.invalidateQueries({ queryKey: ["environment", envId] })
      }
    }
    subs.push(eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATED, refresh))
    subs.push(eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATING, refresh))
    subs.push(eventService.subscribe(EventTypes.ENVIRONMENT_STATUS_CHANGED, refresh))
    return () => { subs.forEach((s) => eventService.unsubscribe(s)) }
  }, [envId, queryClient])

  // Set up request interceptor for blob downloads
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

  // Fetch file content — only once the env is running, since the workspace
  // endpoints reject requests against suspended/stopped envs with HTTP 400.
  const {
    data: fileContent,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["file-content", envId, filePath],
    queryFn: async () => {
      const response = await WorkspaceService.viewWorkspaceFile({
        envId,
        path: filePath,
      })
      return response as unknown as string
    },
    enabled: !!envId && !!filePath && isEnvRunning,
  })

  const handleDownload = async () => {
    try {
      const blob = (await WorkspaceService.downloadWorkspaceItem({
        envId,
        path: filePath,
      })) as unknown as Blob

      saveBlobAs(blob, filename)
    } catch (error) {
      console.error("Download error:", error)
    }
  }

  // Update header
  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center justify-between w-full">
        <div className="flex items-center gap-3 min-w-0">
          <div className="min-w-0">
            <h1 className="text-base font-semibold truncate">{filename}</h1>
            <p className="text-xs text-muted-foreground truncate">{filePath}</p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={handleDownload} className="shrink-0">
          <Download className="h-4 w-4 mr-2" />
          Download
        </Button>
      </div>
    )
    return () => setHeaderContent(null)
  }, [filename, filePath, setHeaderContent])

  if (envStatus && !isEnvRunning) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground">
        <Loader2 className="h-6 w-6 animate-spin" />
        <p className="text-sm">Activating environment ({envStatus})…</p>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">Loading file...</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4">
        <p className="text-destructive">Error loading file</p>
      </div>
    )
  }

  if (!fileContent) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">No content</p>
      </div>
    )
  }

  // Render appropriate viewer based on file type
  if (fileExtension === "csv") {
    return <CSVViewer content={fileContent} />
  }

  if (fileExtension === "md") {
    return <MarkdownViewer content={fileContent} />
  }

  if (fileExtension === "json") {
    return <JSONViewer content={fileContent} />
  }

  if (fileExtension === "txt" || fileExtension === "log") {
    return <TextViewer content={fileContent} />
  }

  // Fallback for unsupported file types
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4">
      <p className="text-muted-foreground">File type not supported for viewing</p>
      <Button variant="outline" onClick={handleDownload}>
        <Download className="h-4 w-4 mr-2" />
        Download File
      </Button>
    </div>
  )
}
