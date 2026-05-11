import { useState, useCallback } from "react"
import { useQueryClient } from "@tanstack/react-query"

import type { AgentPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AgentSchedulesCard } from "./AgentSchedulesCard"
import { AgentHandovers } from "./AgentHandovers"
import { EditDescriptionModal } from "./EditDescriptionModal"
import { EditEntrypointPromptModal } from "./EditEntrypointPromptModal"
import { EditWorkflowPromptModal } from "./EditWorkflowPromptModal"
import { EditRefinerPromptModal } from "./EditRefinerPromptModal"
import { EditExamplePromptsModal } from "./EditExamplePromptsModal"

interface AgentConfigTabProps {
  agent: AgentPublic
  readOnly?: boolean
  /**
   * When false, hides the Schedules + Handovers row. Used in the
   * simplified agent-user view of foreign installs, where only the two
   * informational cards (Information + Agent Prompts) are relevant.
   */
  showOperationalSettings?: boolean
}

export function AgentConfigTab({
  agent,
  readOnly = false,
  showOperationalSettings = true,
}: AgentConfigTabProps) {
  const queryClient = useQueryClient()

  // Modal state
  const [descriptionModalOpen, setDescriptionModalOpen] = useState(false)
  const [entrypointModalOpen, setEntrypointModalOpen] = useState(false)
  const [workflowModalOpen, setWorkflowModalOpen] = useState(false)
  const [refinerModalOpen, setRefinerModalOpen] = useState(false)
  const [examplePromptsModalOpen, setExamplePromptsModalOpen] = useState(false)

  const openWithRefresh = useCallback(
    (setter: (open: boolean) => void) => {
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      setter(true)
    },
    [queryClient, agent.id],
  )

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
            <div className="flex gap-2 flex-wrap">
              <Button
                variant="outline"
                onClick={() => openWithRefresh(setDescriptionModalOpen)}
              >
                Description
              </Button>
              <Button
                variant="outline"
                onClick={() => openWithRefresh(setExamplePromptsModalOpen)}
              >
                Example Prompts
              </Button>
            </div>
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
                onClick={() => openWithRefresh(setEntrypointModalOpen)}
              >
                Entrypoint Prompt
              </Button>
              <Button
                variant="outline"
                onClick={() => openWithRefresh(setWorkflowModalOpen)}
              >
                Workflow Prompt
              </Button>
              <Button
                variant="outline"
                onClick={() => openWithRefresh(setRefinerModalOpen)}
              >
                Refiner Prompt
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Third Row: Scheduler and Handovers (side by side) */}
      {/* Note: Scheduler and Handovers are always editable for agent owner (including clone owners) */}
      {showOperationalSettings && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Schedules Card */}
          <AgentSchedulesCard agentId={agent.id} />

          {/* Handover to Agents - always editable for agent owner (including clone owners) */}
          <AgentHandovers agent={agent} />
        </div>
      )}

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
      <EditExamplePromptsModal
        agentId={agent.id}
        currentPrompts={agent.example_prompts}
        open={examplePromptsModalOpen}
        onClose={() => setExamplePromptsModalOpen(false)}
        readOnly={readOnly}
      />
    </div>
  )
}
