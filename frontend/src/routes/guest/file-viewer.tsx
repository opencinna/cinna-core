import { createFileRoute } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import { WorkspaceService, OpenAPI } from "@/client"
import { saveBlobAs } from "@/utils"
import { CSVViewer } from "@/components/Environment/CSVViewer"
import { MarkdownViewer } from "@/components/Environment/MarkdownViewer"
import { JSONViewer } from "@/components/Environment/JSONViewer"
import { TextViewer } from "@/components/Environment/TextViewer"
import { Button } from "@/components/ui/button"
import { Download, Loader2, AlertCircle } from "lucide-react"
import { useEffect } from "react"
import type { AxiosRequestConfig } from "axios"

interface FileViewerSearch {
  envId: string
  path: string
}

export const Route = createFileRoute("/guest/file-viewer")({
  component: GuestFileViewerPage,
  validateSearch: (search: Record<string, unknown>): FileViewerSearch => ({
    envId: search.envId as string,
    path: search.path as string,
  }),
  head: () => ({
    meta: [{ title: "File Viewer" }],
  }),
})

function GuestFileViewerPage() {
  const { envId, path } = Route.useSearch()

  const filename = path.split("/").pop() || "file"
  const fileExtension = filename.split(".").pop()?.toLowerCase()

  // Set up request interceptor for blob downloads
  useEffect(() => {
    const interceptor = (config: AxiosRequestConfig) => {
      if (config.url?.includes("/workspace/download/")) {
        config.responseType = "blob"
      }
      return config
    }
    OpenAPI.interceptors.request.use(interceptor)
    return () => {
      OpenAPI.interceptors.request.eject(interceptor)
    }
  }, [])

  const {
    data: fileContent,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["file-content", envId, path],
    queryFn: async () => {
      const response = await WorkspaceService.viewWorkspaceFile({
        envId,
        path,
      })
      return response as unknown as string
    },
    enabled: !!envId && !!path,
  })

  const handleDownload = async () => {
    try {
      const blob = (await WorkspaceService.downloadWorkspaceItem({
        envId,
        path,
      })) as unknown as Blob

      saveBlobAs(blob, filename)
    } catch (error) {
      console.error("Download error:", error)
    }
  }

  return (
    <div className="flex flex-col h-screen bg-background">
      {/* Header */}
      <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center justify-between gap-4 border-b px-4 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        <div className="min-w-0">
          <h1 className="text-sm font-semibold truncate">{filename}</h1>
          <p className="text-xs text-muted-foreground truncate">{path}</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleDownload}
          className="shrink-0"
        >
          <Download className="h-4 w-4 mr-2" />
          Download
        </Button>
      </header>

      {/* Content */}
      <main className="flex-1 min-h-0 overflow-auto">
        {isLoading ? (
          <div className="flex items-center justify-center h-full">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : error ? (
          <div className="flex flex-col items-center justify-center h-full gap-3">
            <AlertCircle className="h-8 w-8 text-destructive" />
            <p className="text-destructive text-sm">Error loading file</p>
          </div>
        ) : !fileContent ? (
          <div className="flex items-center justify-center h-full">
            <p className="text-muted-foreground">No content</p>
          </div>
        ) : fileExtension === "csv" ? (
          <CSVViewer content={fileContent} />
        ) : fileExtension === "md" ? (
          <MarkdownViewer content={fileContent} />
        ) : fileExtension === "json" ? (
          <JSONViewer content={fileContent} />
        ) : fileExtension === "txt" || fileExtension === "log" ? (
          <TextViewer content={fileContent} />
        ) : (
          <div className="flex flex-col items-center justify-center h-full gap-4">
            <p className="text-muted-foreground">
              File type not supported for viewing
            </p>
            <Button variant="outline" onClick={handleDownload}>
              <Download className="h-4 w-4 mr-2" />
              Download File
            </Button>
          </div>
        )}
      </main>
    </div>
  )
}
