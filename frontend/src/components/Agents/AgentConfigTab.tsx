import { useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import type { AgentPublic } from "@/client"
import { AgentsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { SmartScheduler } from "./SmartScheduler"
import { AgentHandovers } from "./AgentHandovers"
import { EditDescriptionModal } from "./EditDescriptionModal"
import { EditEntrypointPromptModal } from "./EditEntrypointPromptModal"
import { EditWorkflowPromptModal } from "./EditWorkflowPromptModal"
import { EditRefinerPromptModal } from "./EditRefinerPromptModal"

interface AgentConfigTabProps {
  agent: AgentPublic
  readOnly?: boolean
}

export function AgentConfigTab({ agent, readOnly = false }: AgentConfigTabProps) {
  // Modal state
  const [descriptionModalOpen, setDescriptionModalOpen] = useState(false)
  const [entrypointModalOpen, setEntrypointModalOpen] = useState(false)
  const [workflowModalOpen, setWorkflowModalOpen] = useState(false)
  const [refinerModalOpen, setRefinerModalOpen] = useState(false)

  // Scheduler state
  const [schedulerEnabled, setSchedulerEnabled] = useState(false)

  // Fetch current schedule
  const { data: schedule, refetch: refetchSchedule } = useQuery({
    queryKey: ["agentSchedule", agent.id],
    queryFn: () => AgentsService.getSchedule({ id: agent.id }),
    enabled: !!agent.id,
  })

  // Sync scheduler enabled state with fetched schedule
  useEffect(() => {
    if (schedule) {
      setSchedulerEnabled(schedule.enabled)
    }
  }, [schedule])

  const handleSchedulerToggle = (checked: boolean) => {
    setSchedulerEnabled(checked)
  }

  return (
    <div className="space-y-6">
      {/* Top Row: Information and Agent Prompts (side by side) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Information Card */}
        <Card>
          <CardHeader>
            <CardTitle>Information</CardTitle>
            <CardDescription>
              Basic information about this agent
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              variant="outline"
              onClick={() => setDescriptionModalOpen(true)}
            >
              Description
            </Button>
          </CardContent>
        </Card>

        {/* Agent Prompts Card */}
        <Card>
          <CardHeader>
            <CardTitle>Agent Prompts</CardTitle>
            <CardDescription>
              Configure the prompts that define how this agent behaves
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex gap-2 flex-wrap">
              <Button
                variant="outline"
                onClick={() => setEntrypointModalOpen(true)}
              >
                Entrypoint Prompt
              </Button>
              <Button
                variant="outline"
                onClick={() => setWorkflowModalOpen(true)}
              >
                Workflow Prompt
              </Button>
              <Button
                variant="outline"
                onClick={() => setRefinerModalOpen(true)}
              >
                Refiner Prompt
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Third Row: Scheduler and Handovers (side by side) */}
      {/* Note: Scheduler and Handovers are always editable for agent owner (including clone owners) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Scheduler Card */}
        <Card>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div className="space-y-1.5">
                  <CardTitle>Scheduler</CardTitle>
                  <CardDescription>
                    Schedule execution time for this agent with entrypoint prompt as
                    starting message
                  </CardDescription>
                </div>
                <label className="flex cursor-pointer select-none items-center ml-4 mt-1">
                  <div className="relative">
                    <input
                      type="checkbox"
                      checked={schedulerEnabled}
                      onChange={(e) => handleSchedulerToggle(e.target.checked)}
                      className="sr-only"
                    />
                    <div
                      className={`block h-6 w-11 rounded-full transition-colors ${
                        schedulerEnabled ? "bg-emerald-500" : "bg-gray-300 dark:bg-gray-600"
                      }`}
                    ></div>
                    <div
                      className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                        schedulerEnabled ? "translate-x-5" : ""
                      }`}
                    ></div>
                  </div>
                </label>
              </div>
            </CardHeader>
            <CardContent>
              <SmartScheduler
                agentId={agent.id}
                currentSchedule={schedule ?? undefined}
                onScheduleUpdate={() => refetchSchedule()}
                enabled={schedulerEnabled}
                onToggle={handleSchedulerToggle}
              />
            </CardContent>
          </Card>

        {/* Handover to Agents - always editable for agent owner (including clone owners) */}
        <AgentHandovers agent={agent} />
      </div>

      {/* Modals */}
      <EditDescriptionModal
        agentId={agent.id}
        currentDescription={agent.description}
        open={descriptionModalOpen}
        onClose={() => setDescriptionModalOpen(false)}
        readOnly={readOnly}
      />
      <EditEntrypointPromptModal
        agentId={agent.id}
        currentPrompt={agent.entrypoint_prompt}
        open={entrypointModalOpen}
        onClose={() => setEntrypointModalOpen(false)}
        readOnly={readOnly}
      />
      <EditWorkflowPromptModal
        agentId={agent.id}
        currentPrompt={agent.workflow_prompt}
        open={workflowModalOpen}
        onClose={() => setWorkflowModalOpen(false)}
        readOnly={readOnly}
      />
      <EditRefinerPromptModal
        agentId={agent.id}
        currentPrompt={agent.refiner_prompt}
        open={refinerModalOpen}
        onClose={() => setRefinerModalOpen(false)}
        readOnly={readOnly}
      />
    </div>
  )
}
