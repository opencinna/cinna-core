import { useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Plus, Workflow } from "lucide-react"

import type { AgentPublic, HandoverConfigPublic } from "@/client"
import { AgentsService } from "@/client"
import type { AgentOption } from "@/components/Common/AgentSelectorDialog"
import { AgentSelectorDialog } from "@/components/Common/AgentSelectorDialog"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { handleError } from "@/utils"
import { AllHandoversSheet } from "./AllHandoversSheet"
import { EditHandoverPromptModal } from "./EditHandoverPromptModal"
import { HandoverRow } from "./HandoverRow"

/** Rows shown in the card; the rest live behind "Show all (N)". */
const PREVIEW_COUNT = 5

/** Stable identity so the derivations below memoise while the query loads. */
const NO_HANDOVERS: HandoverConfigPublic[] = []

interface AgentHandoversProps {
  agent: AgentPublic
}

/**
 * "Handover to Agents" — which agents this one can hand work to.
 *
 * A View surface (guidelines §1): the card answers "who, is it live, and
 * roughly what does it pass" at a glance. Every mutation is one level down —
 * the row's `⋯` menu, the prompt dialog, the delete confirm — so nothing on
 * the card changes its height, and the card holds one interaction model
 * (auto-save) while the only form lives in the dialog.
 *
 * Owner-only: the host renders it under `showOperationalSettings` and foreign
 * installs never see it, so there is no read-only variant.
 */
export function AgentHandovers({ agent }: AgentHandoversProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { workspaceFilter } = useWorkspace()

  const [isPickerOpen, setIsPickerOpen] = useState(false)
  const [isSheetOpen, setIsSheetOpen] = useState(false)
  // The handover just created by the picker: the Create story ends in the
  // prompt editor rather than on a half-configured row.
  const [createdHandover, setCreatedHandover] =
    useState<HandoverConfigPublic | null>(null)

  // Agents are fetched for the picker's available list; the same query tints
  // each row's icon tile, since the handover projection carries no colour.
  const {
    data: agentsData,
    isLoading: isAgentsLoading,
    isError: isAgentsError,
  } = useQuery({
    queryKey: ["agents", workspaceFilter],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId as string | undefined,
      })
    },
  })

  const {
    data: handoversData,
    isLoading,
    isError,
    refetch,
  } = useQuery({
    queryKey: ["agentHandovers", agent.id],
    queryFn: () => AgentsService.listHandoverConfigs({ id: agent.id }),
    enabled: !!agent.id,
  })

  const createMutation = useMutation({
    mutationFn: (targetAgentId: string) =>
      AgentsService.createHandoverConfig({
        id: agent.id,
        requestBody: { target_agent_id: targetAgentId, handover_prompt: "" },
      }),
    onSuccess: (created) => {
      showSuccessToast(`Handover to ${created.target_agent_name} added`)
      queryClient.invalidateQueries({ queryKey: ["agentHandovers", agent.id] })
      setCreatedHandover(created)
    },
    onError: handleError.bind(showErrorToast),
  })

  const handovers = handoversData?.data ?? NO_HANDOVERS
  const totalCount = handoversData?.count ?? handovers.length

  // Most relevant first: live handovers, then most recently changed.
  const sortedHandovers = useMemo(
    () =>
      [...handovers].sort((a, b) => {
        if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
        return (
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
        )
      }),
    [handovers],
  )

  const colorPresetByAgentId = useMemo(() => {
    const map: Record<string, string | null | undefined> = {}
    for (const a of agentsData?.data ?? []) map[a.id] = a.ui_color_preset
    return map
  }, [agentsData])

  // Self and already-configured targets cannot be picked again.
  const availableAgents = useMemo(
    () =>
      (agentsData?.data ?? []).filter(
        (a) =>
          a.id !== agent.id &&
          !handovers.some((h) => h.target_agent_id === a.id),
      ),
    [agentsData, agent.id, handovers],
  )

  const canAdd = availableAgents.length > 0
  // Nothing may be asserted about the agent list until it has actually
  // arrived: while it is loading or failed, `availableAgents` is empty for
  // reasons that have nothing to do with how many agents exist.
  const agentsKnown = !isAgentsLoading && !isAgentsError
  const hasOtherAgents = (agentsData?.data ?? []).some((a) => a.id !== agent.id)
  const cannotAddReason = isAgentsLoading
    ? "Loading agents…"
    : isAgentsError
      ? "Couldn't load the list of agents"
      : hasOtherAgents
        ? "Every other agent already has a handover"
        : "No other agents available"

  const addButton = (
    <Button size="sm" disabled={!canAdd} onClick={() => setIsPickerOpen(true)}>
      <Plus className="mr-2 h-4 w-4" />
      Add handover
    </Button>
  )

  return (
    <Card>
      <CardHeader>
        {/* Title and action share one row; the description spans the full
            header width below them, so it stays two lines at 1024 instead of
            being squeezed into a column beside the button. */}
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <Workflow className="h-5 w-5 shrink-0" />
            Handover to Agents
          </CardTitle>
          <div className="shrink-0">
            {canAdd ? (
              addButton
            ) : (
              <Tooltip>
                {/* A disabled button fires no pointer events of its own. */}
                <TooltipTrigger asChild>
                  <span tabIndex={0}>{addButton}</span>
                </TooltipTrigger>
                <TooltipContent side="top" className="text-xs">
                  {cannotAddReason}
                </TooltipContent>
              </Tooltip>
            )}
          </div>
        </div>
        <CardDescription>
          Agents this one can hand work to, and the context it passes.
        </CardDescription>
      </CardHeader>

      <CardContent>
        {isError ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load handovers</AlertTitle>
            <AlertDescription className="flex flex-col items-start gap-2">
              <span>
                The handover configuration for this agent could not be fetched.
              </span>
              <Button variant="outline" size="sm" onClick={() => refetch()}>
                Retry
              </Button>
            </AlertDescription>
          </Alert>
        ) : isLoading ? (
          <div className="space-y-1.5">
            <Skeleton className="h-[52px] w-full rounded-lg" />
            <Skeleton className="h-[52px] w-full rounded-lg" />
            <Skeleton className="h-[52px] w-full rounded-lg" />
          </div>
        ) : sortedHandovers.length === 0 ? (
          agentsKnown && !hasOtherAgents ? (
            <p className="text-sm text-muted-foreground">
              There are no other agents to hand work to.{" "}
              <Link to="/agents" className="text-primary hover:underline">
                Create another agent
              </Link>
              .
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">
              This agent doesn&apos;t hand work to any other agent yet.
            </p>
          )
        ) : (
          <div className="space-y-1.5">
            {sortedHandovers.slice(0, PREVIEW_COUNT).map((handover) => (
              <HandoverRow
                key={handover.id}
                agentId={agent.id}
                handover={handover}
                targetColorPreset={
                  colorPresetByAgentId[handover.target_agent_id]
                }
              />
            ))}
            {totalCount > PREVIEW_COUNT && (
              <Button
                variant="link"
                size="sm"
                className="px-0"
                onClick={() => setIsSheetOpen(true)}
              >
                Show all ({totalCount})
              </Button>
            )}
          </div>
        )}
      </CardContent>

      {/* S2 — the picker returns one agent and closes itself, then the prompt
          editor opens on the new handover. Sequential, never nested. */}
      <AgentSelectorDialog
        open={isPickerOpen}
        onOpenChange={setIsPickerOpen}
        onSelect={(agentId) => createMutation.mutate(agentId)}
        agents={availableAgents.map(
          (a): AgentOption => ({
            id: a.id,
            name: a.name,
            colorPreset: a.ui_color_preset,
          }),
        )}
        title="Add handover"
      />

      {createdHandover && (
        <EditHandoverPromptModal
          agentId={agent.id}
          handoverId={createdHandover.id}
          targetAgentId={createdHandover.target_agent_id}
          targetAgentName={createdHandover.target_agent_name}
          currentPrompt={createdHandover.handover_prompt}
          open
          onClose={() => setCreatedHandover(null)}
        />
      )}

      <AllHandoversSheet
        agentId={agent.id}
        handovers={sortedHandovers}
        totalCount={totalCount}
        colorPresetByAgentId={colorPresetByAgentId}
        open={isSheetOpen}
        onOpenChange={setIsSheetOpen}
      />
    </Card>
  )
}
