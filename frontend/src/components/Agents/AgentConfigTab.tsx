import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useState } from "react"

import type { AgentPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AgentHandovers } from "./AgentHandovers"
import { AgentSchedulesCard } from "./AgentSchedulesCard"
import { AgentStatusCard } from "./AgentStatusCard"
import { BundleInstallationCard } from "./BundleInstallationCard"
import { EditDescriptionModal } from "./EditDescriptionModal"
import { EditEntrypointPromptModal } from "./EditEntrypointPromptModal"
import { EditExamplePromptsModal } from "./EditExamplePromptsModal"
import { EditRefinerPromptModal } from "./EditRefinerPromptModal"
import { EditRouterTriggerPromptModal } from "./EditRouterTriggerPromptModal"
import { EditWorkflowPromptModal } from "./EditWorkflowPromptModal"
import { ImprovementRequestsCard } from "./ImprovementRequestsCard"

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
  const [triggerPromptModalOpen, setTriggerPromptModalOpen] = useState(false)
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
      {/* One shared responsive grid for every card on the tab — two columns
          from ``lg`` up, one below. Cards are direct grid items and flow into
          the next free cell, so a conditionally hidden card (a null child
          occupies no cell) closes the gap instead of stranding its neighbour
          on a row of its own. Anything added here must therefore stay a bare
          Card, not a card wrapped in its own grid. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
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
              <Button
                variant="outline"
                onClick={() => openWithRefresh(setTriggerPromptModalOpen)}
              >
                Trigger Prompt
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* Bundle installation — foreign installs only (the card self-hides via
            the same ``bundle_uuid && !is_publisher_install`` rule that makes
            this tab read-only). Intentionally NOT passed ``readOnly``: the
            update mode is the consumer's own preference, not publisher-authored
            content. */}
        <BundleInstallationCard agent={agent} />

        {/* Schedules render for the agent owner (editable) and for foreign
            installs (read-only): bundle publishers can ship schedules, and the
            consumer may enable/disable, run, and view logs but not edit them.
            Handovers stay owner-only — they're not bundle-propagated. */}
        {(showOperationalSettings || readOnly) && (
          <AgentSchedulesCard agentId={agent.id} readOnly={readOnly} />
        )}

        {/* Handover to Agents — owner-only, not shown on foreign installs */}
        {showOperationalSettings && <AgentHandovers agent={agent} />}

        {/* Agent status — self-reported health + the refresh command.
            Owner-facing configuration (editable command), so it follows the
            same developer-tier gate as Handovers. */}
        {showOperationalSettings && <AgentStatusCard agent={agent} />}

        {/* Improvement requests. Deliberately NOT gated on
            ``showOperationalSettings`` or the developer role: reading feedback
            people sent about an agent you own is an owner capability, and a
            plain agent-user owning a standalone agent must still see it.

            Rendered on foreign installs too, but only once something has
            arrived. Requests raised against a bundle install normally route to
            the publisher — except when the publisher install is gone, where
            ``resolve_target`` falls back to self and the row lands on the
            consumer's own install. Gating that away on ``readOnly`` made those
            requests invisible to the only person who could act on them. */}
        <ImprovementRequestsCard agentId={agent.id} hideWhenEmpty={readOnly} />
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
      {/* Trigger Prompt is editable for any install owner — including
          foreign installs — because the backend
          ``PATCH /agents/{id}/router-trigger-prompt`` endpoint bypasses
          the read-only/developer gate. It's the installer's own routing
          configuration, not bundle-authored content. */}
      <EditRouterTriggerPromptModal
        agentId={agent.id}
        currentPrompt={agent.router_trigger_prompt}
        open={triggerPromptModalOpen}
        onClose={() => setTriggerPromptModalOpen(false)}
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
