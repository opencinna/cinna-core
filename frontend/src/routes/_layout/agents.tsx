import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Search } from "lucide-react"
import { Suspense } from "react"

import { AgentsService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import AddAgent from "@/components/Agents/AddAgent"
import { columns } from "@/components/Agents/columns"
import PendingItems from "@/components/Pending/PendingItems"

function getAgentsQueryOptions() {
  return {
    queryFn: () => AgentsService.readAgents({ skip: 0, limit: 100 }),
    queryKey: ["agents"],
  }
}

export const Route = createFileRoute("/_layout/agents")({
  component: Agents,
  head: () => ({
    meta: [
      {
        title: "Agents - FastAPI Cloud",
      },
    ],
  }),
})

function AgentsTableContent() {
  const { data: agents } = useSuspenseQuery(getAgentsQueryOptions())

  if (agents.data.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12">
        <div className="rounded-full bg-muted p-4 mb-4">
          <Search className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold">You don't have any agents yet</h3>
        <p className="text-muted-foreground">Add a new agent to get started</p>
      </div>
    )
  }

  return <DataTable columns={columns} data={agents.data} />
}

function AgentsTable() {
  return (
    <Suspense fallback={<PendingItems />}>
      <AgentsTableContent />
    </Suspense>
  )
}

function Agents() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Agents</h1>
          <p className="text-muted-foreground">Create and manage your agents</p>
        </div>
        <AddAgent />
      </div>
      <AgentsTable />
    </div>
  )
}
