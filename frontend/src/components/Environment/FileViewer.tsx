import { useQuery } from "@tanstack/react-query"
import { Download } from "lucide-react"
import { Button } from "@/components/ui/button"
import { WorkspaceService, OpenAPI } from "@/client"
import { CSVViewer } from "./CSVViewer"
import { MarkdownViewer } from "./MarkdownViewer"
import { JSONViewer } from "./JSONViewer"
import { TextViewer } from "./TextViewer"
import { useEffect } from "react"
import { usePageHeader } from "@/routes/_layout"
import type { AxiosRequestConfig } from "axios"

interface FileViewerProps {
  envId: string
  filePath: string
}

export function FileViewer({ envId, filePath }: FileViewerProps) {
  const { setHeaderContent } = usePageHeader()

  // Extract filename from path
  const filename = filePath.split("/").pop() || "file"
  const fileExtension = filename.split(".").pop()?.toLowerCase()

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

  // Fetch file content
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
    enabled: !!envId && !!filePath,
  })

  const handleDownload = async () => {
    try {
      const blob = (await WorkspaceService.downloadWorkspaceItem({
        envId,
        path: filePath,
      })) as unknown as Blob

      const url = window.URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      window.URL.revokeObjectURL(url)
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
