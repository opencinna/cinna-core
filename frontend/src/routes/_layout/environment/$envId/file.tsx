import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { FileViewer } from "@/components/Environment/FileViewer"

const fileSearchSchema = z.object({
  path: z.string(),
})

export const Route = createFileRoute("/_layout/environment/$envId/file")({
  validateSearch: fileSearchSchema,
  component: FileViewerPage,
})

function FileViewerPage() {
  const { envId } = Route.useParams()
  const { path } = Route.useSearch()

  return <FileViewer envId={envId} filePath={path} />
}
