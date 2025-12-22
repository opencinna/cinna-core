import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Bot } from "lucide-react"

import { AgentsService } from "@/client"
import AddAgent from "@/components/Agents/AddAgent"
import { AgentCard } from "@/components/Agents/AgentCard"
import PendingItems from "@/components/Pending/PendingItems"

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

function AgentsGrid() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["agents"],
    queryFn: async () => {
      const response = await AgentsService.readAgents({
        skip: 0,
        limit: 100,
      })
      return response
    },
  })

  if (isLoading) {
    return <PendingItems />
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading agents: {(error as Error).message}
        </p>
      </div>
    )
  }

  const agents = data?.data || []

  if (agents.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12">
        <div className="rounded-full bg-muted p-4 mb-4">
          <Bot className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold">
          You don't have any agents yet
        </h3>
        <p className="text-muted-foreground">Add a new agent to get started</p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      {agents.map((agent) => (
        <AgentCard key={agent.id} agent={agent} />
      ))}
    </div>
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
      <AgentsGrid />
    </div>
  )
}
