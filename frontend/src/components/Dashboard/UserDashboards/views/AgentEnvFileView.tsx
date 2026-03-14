import { useQuery } from "@tanstack/react-query"
import { AlertCircle, Loader2 } from "lucide-react"
import { DashboardsService } from "@/client"
import { CSVViewer } from "@/components/Environment/CSVViewer"
import { MarkdownViewer } from "@/components/Environment/MarkdownViewer"
import { JSONViewer } from "@/components/Environment/JSONViewer"
import { TextViewer } from "@/components/Environment/TextViewer"

interface AgentEnvFileViewProps {
  dashboardId: string
  blockId: string
  filePath: string
}

export function AgentEnvFileView({
  dashboardId,
  blockId,
  filePath,
}: AgentEnvFileViewProps) {
  const filename = filePath.split("/").pop() || filePath
  const fileExtension = filename.split(".").pop()?.toLowerCase()

  const {
    data: fileContent,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["dashboardBlockEnvFile", dashboardId, blockId, filePath],
    queryFn: async () => {
      const response = await DashboardsService.getBlockEnvFile({
        dashboardId,
        blockId,
        path: filePath,
      })
      return response as unknown as string
    },
    enabled: !!dashboardId && !!blockId && !!filePath,
    refetchInterval: 30000,
    staleTime: 15000,
  })

  if (!filePath) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        No file configured
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span className="text-sm">Loading file...</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-2 text-muted-foreground p-4">
        <AlertCircle className="h-5 w-5 text-destructive shrink-0" />
        <span className="text-sm text-center">Failed to load file</span>
      </div>
    )
  }

  if (!fileContent) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        Empty file
      </div>
    )
  }

  if (fileExtension === "csv") {
    return <CSVViewer content={fileContent} />
  }

  if (fileExtension === "md") {
    return <MarkdownViewer content={fileContent} />
  }

  if (fileExtension === "json") {
    return <JSONViewer content={fileContent} />
  }

  // txt, log, and any other text-like extension
  return <TextViewer content={fileContent} />
}
