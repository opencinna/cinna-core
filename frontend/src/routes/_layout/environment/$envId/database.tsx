import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { DatabaseViewer } from "@/components/Environment/DatabaseViewer"

const databaseSearchSchema = z.object({
  path: z.string(),
  table: z.string().optional(),
})

export const Route = createFileRoute("/_layout/environment/$envId/database")({
  validateSearch: databaseSearchSchema,
  component: DatabaseViewerPage,
})

function DatabaseViewerPage() {
  const { envId } = Route.useParams()
  const { path, table } = Route.useSearch()

  return <DatabaseViewer envId={envId} dbPath={path} initialTable={table} />
}
