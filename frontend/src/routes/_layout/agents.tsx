import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Bot } from "lucide-react"
import { useEffect, useState } from "react"

import { AgentsService, AgentSharesService } from "@/client"
import type { PendingSharePublic } from "@/client"
import AddAgent from "@/components/Agents/AddAgent"
import { AgentCard } from "@/components/Agents/AgentCard"
import { PendingAgentCard } from "@/components/Agents/PendingAgentCard"
import { AcceptShareWizard } from "@/components/Agents/AcceptShareWizard"
import PendingItems from "@/components/Pending/PendingItems"
import useWorkspace from "@/hooks/useWorkspace"
import { usePageHeader } from "@/routes/_layout"
import useCustomToast from "@/hooks/useCustomToast"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/agents")({
  component: Agents,
  head: () => ({
    meta: [
      {
        title: `Agents - ${APP_NAME}`,
      },
    ],
  }),
})

function AgentsGrid() {
  const { activeWorkspaceId } = useWorkspace()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [selectedShare, setSelectedShare] = useState<PendingSharePublic | null>(
    null
  )
  const [wizardOpen, setWizardOpen] = useState(false)

  // Fetch owned agents (existing query)
  const {
    data,
    isLoading: agentsLoading,
    error: agentsError,
  } = useQuery({
    queryKey: ["agents", activeWorkspaceId],
    queryFn: async ({ queryKey }) => {
      const [, workspaceId] = queryKey
      const response = await AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId ?? "",
      })
      return response
    },
  })

  // Fetch status snapshots for all agents in one batch (cache-only, cheap).
  const { data: statusesData } = useQuery({
    queryKey: ["agentStatuses", activeWorkspaceId],
    queryFn: () =>
      AgentsService.listAgentStatuses({
        workspaceId: activeWorkspaceId ?? undefined,
      }),
    staleTime: 30_000,
  })
  const statusByAgentId = new Map(
    (statusesData?.items ?? []).map((s) => [s.agent_id, s]),
  )

  // Fetch pending shares (new query)
  const { data: pendingSharesData, isLoading: pendingLoading } = useQuery({
    queryKey: ["pendingShares"],
    queryFn: () => AgentSharesService.getPendingShares(),
  })

  // Decline mutation
  const declineMutation = useMutation({
    mutationFn: (shareId: string) =>
      AgentSharesService.declineShare({ shareId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pendingShares"] })
      showSuccessToast("Share declined")
    },
    onError: (error) => {
      showErrorToast(
        error instanceof Error ? error.message : "Failed to decline share"
      )
    },
  })

  const handleAcceptClick = (share: PendingSharePublic) => {
    setSelectedShare(share)
    setWizardOpen(true)
  }

  const handleDeclineClick = (shareId: string) => {
    if (confirm("Are you sure you want to decline this share?")) {
      declineMutation.mutate(shareId)
    }
  }

  const handleWizardComplete = () => {
    setWizardOpen(false)
    setSelectedShare(null)
    queryClient.invalidateQueries({ queryKey: ["agents"] })
    queryClient.invalidateQueries({ queryKey: ["pendingShares"] })
    showSuccessToast("Agent accepted successfully")
  }

  if (agentsLoading || pendingLoading) {
    return <PendingItems />
  }

  if (agentsError) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading agents: {(agentsError as Error).message}
        </p>
      </div>
    )
  }

  const agents = data?.data || []
  const pendingShares = pendingSharesData?.data || []

  const hasNoContent = agents.length === 0 && pendingShares.length === 0

  if (hasNoContent) {
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
    <>
      {/* Pending Shares Section */}
      {pendingShares.length > 0 && (
        <div className="mb-8">
          <h2 className="text-lg font-semibold mb-4 text-muted-foreground flex items-center gap-2">
            Pending Shared Agents
            <span className="inline-flex items-center justify-center w-6 h-6 text-xs font-medium rounded-full bg-muted text-muted-foreground">
              {pendingShares.length}
            </span>
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
            {pendingShares.map((share) => (
              <PendingAgentCard
                key={share.id}
                share={share}
                onAccept={() => handleAcceptClick(share)}
                onDecline={() => handleDeclineClick(share.id)}
                isLoading={declineMutation.isPending}
              />
            ))}
          </div>
        </div>
      )}

      {/* Owned Agents Section */}
      {agents.length > 0 && (
        <div>
          {pendingShares.length > 0 && (
            <h2 className="text-lg font-semibold mb-4">
              Your Agents ({agents.length})
            </h2>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
            {agents.map((agent) => (
              <AgentCard
                key={agent.id}
                agent={agent}
                status={statusByAgentId.get(agent.id) ?? null}
              />
            ))}
          </div>
        </div>
      )}

      {/* Accept Wizard Dialog */}
      {selectedShare && (
        <AcceptShareWizard
          open={wizardOpen}
          onOpenChange={setWizardOpen}
          share={selectedShare}
          onComplete={handleWizardComplete}
        />
      )}
    </>
  )
}

function Agents() {
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Agents</h1>
          <p className="text-xs text-muted-foreground">Create and manage your agents</p>
        </div>
        <AddAgent />
      </>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <AgentsGrid key={activeWorkspaceId ?? 'default'} />
      </div>
    </div>
  )
}
