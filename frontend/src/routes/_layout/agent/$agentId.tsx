import { useQuery } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, EllipsisVertical } from "lucide-react"
import { useState } from "react"

import { AgentsService } from "@/client"
import { AgentPromptsTab } from "@/components/Agents/AgentPromptsTab"
import { AgentCredentialsTab } from "@/components/Agents/AgentCredentialsTab"
import { AgentEnvironmentsTab } from "@/components/Agents/AgentEnvironmentsTab"
import EditAgent from "@/components/Agents/EditAgent"
import DeleteAgent from "@/components/Agents/DeleteAgent"
import PendingItems from "@/components/Pending/PendingItems"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

// Custom tab button component
interface TabButtonProps {
  isActive: boolean
  onClick: () => void
  children: React.ReactNode
}

function TabButton({ isActive, onClick, children }: TabButtonProps) {
  return (
    <Button
      variant="ghost"
      className={`
        border-b-2 rounded-none px-4 py-2
        ${isActive ? "border-primary text-primary" : "border-transparent"}
      `}
      onClick={onClick}
    >
      {children}
    </Button>
  )
}

export const Route = createFileRoute("/_layout/agent/$agentId")({
  component: AgentDetail,
})

function AgentDetail() {
  const { agentId } = Route.useParams()
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState<"prompts" | "credentials" | "environments">(
    "prompts"
  )
  const [menuOpen, setMenuOpen] = useState(false)

  const {
    data: agent,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => AgentsService.readAgent({ id: agentId }),
    enabled: !!agentId,
  })

  const handleDeleteSuccess = () => {
    navigate({ to: "/agents" })
  }

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !agent) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading agent details</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => navigate({ to: "/agents" })}
          >
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">{agent.name}</h1>
          </div>
        </div>
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon">
              <EllipsisVertical />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <EditAgent agent={agent} onSuccess={() => setMenuOpen(false)} />
            <DeleteAgent
              id={agent.id}
              onSuccess={handleDeleteSuccess}
            />
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Tabs Section */}
      <div>
        <div className="flex border-b mb-4">
          <TabButton
            isActive={activeTab === "prompts"}
            onClick={() => setActiveTab("prompts")}
          >
            Prompts
          </TabButton>
          <TabButton
            isActive={activeTab === "credentials"}
            onClick={() => setActiveTab("credentials")}
          >
            Credentials
          </TabButton>
          <TabButton
            isActive={activeTab === "environments"}
            onClick={() => setActiveTab("environments")}
          >
            Environments
          </TabButton>
        </div>

        <div>
          {activeTab === "prompts" && <AgentPromptsTab agent={agent} />}
          {activeTab === "credentials" && (
            <AgentCredentialsTab agentId={agent.id} />
          )}
          {activeTab === "environments" && (
            <AgentEnvironmentsTab agentId={agent.id} />
          )}
        </div>
      </div>
    </div>
  )
}
