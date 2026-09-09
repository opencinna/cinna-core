import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { ChevronDown, ChevronRight, MessageCircle, Wrench } from "lucide-react"
import { useMemo, useState } from "react"

import { AgentsService, SkillsService } from "@/client"
import { AgentSelectorList } from "@/components/Common/AgentSelectorDialog"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"
import { invalidateAddons } from "@/utils/addons"
import { skillRevisionLabel } from "@/utils/skillCatalog"
import { SkillCatalogErrorAlert } from "./SkillCatalogErrorAlert"

interface AddSkillToAgentDialogProps {
  /** The package's UUID — every skills route keys on it, not on the handle. */
  packageId: string
  packageName: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Put this catalog skill into one of my agents" — S10.
 *
 * A Create story with three fields, so it is one dialog rather than a wizard,
 * built to the same skeleton as the Addons tab's install step: the two mode `Checkbox`es
 * are literally the same control the marketplace install uses, and a skill that
 * installs differently from a plugin — when the backend makes it *one* plugin
 * link either way — would be a second vocabulary for one act.
 *
 * The agent picker is `Common/AgentSelectorList` in `single` mode — a select
 * trigger carrying the chosen agent's badge, over the same search-and-pick
 * cloud the other eight agent-choosing surfaces use, colour presets and all,
 * which is how an agent is recognised at a glance rather than read off a list
 * of names. One field, one line, and the list of every agent stays out of a
 * form whose next field is the real question. The picker is a `Popover`
 * anchored to the field, never a second `Dialog` on top of this one (§2
 * "Disclosure depth").
 */
export function AddSkillToAgentDialog({
  packageId,
  packageName,
  open,
  onOpenChange,
}: AddSkillToAgentDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast } = useCustomToast()

  const [agentId, setAgentId] = useState("")
  const [conversationMode, setConversationMode] = useState(true)
  const [buildingMode, setBuildingMode] = useState(true)
  const [revisionNumber, setRevisionNumber] = useState<string>("latest")
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const {
    data: agentsData,
    isLoading: isLoadingAgents,
    isError: isAgentsError,
    refetch: refetchAgents,
  } = useQuery({
    queryKey: ["agents"],
    queryFn: () => AgentsService.readAgents(),
    enabled: open,
  })

  // The revision list only exists on the detail payload, and the grid's card
  // holds an entry. Fetching it here keeps one dialog usable from both hosts
  // instead of threading revisions down through the grid.
  const { data: pkg } = useQuery({
    queryKey: ["skills-catalog", "package", packageId],
    queryFn: () => SkillsService.getSkillPackage({ packageId }),
    enabled: open,
  })

  const agents = agentsData?.data ?? []
  // The picker's own shape: it renders the colour pill, so it needs the preset
  // rather than a value/label pair.
  const agentOptions = useMemo(
    () =>
      agents.map((agent) => ({
        id: agent.id,
        name: agent.name,
        colorPreset: agent.ui_color_preset,
      })),
    [agents],
  )
  const selectedAgent = agents.find((agent) => agent.id === agentId)
  const revisions = pkg?.revisions ?? []
  const latestLabel = pkg?.latest_revision
    ? skillRevisionLabel(pkg.latest_revision)
    : null

  const installMutation = useMutation({
    mutationFn: () =>
      SkillsService.installAgentSkill({
        agentId,
        requestBody: {
          package_id: packageId,
          revision_number:
            revisionNumber === "latest" ? null : Number(revisionNumber),
          conversation_mode: conversationMode,
          building_mode: buildingMode,
        },
      }),
    onSuccess: () => {
      showSuccessToast(
        `Added ${packageName} to ${selectedAgent?.name ?? "the agent"}`,
      )
      // Everything an install touches, through the one helper: the Addons
      // tab's projection, the plugin-link list, the agent's skill index and
      // the catalog's own `installed_in_agent_ids` / `install_count`. Naming
      // the keys here instead is how this dialog silently stopped refreshing
      // the projection it had just made stale.
      invalidateAddons(queryClient, agentId, { catalog: true })
      onOpenChange(false)
    },
    // No `onError`: the refusal is coded and belongs in the dialog, where the
    // field that caused it still is. A toast would take it off screen.
  })

  const isPending = installMutation.isPending
  const canSubmit =
    !!agentId && (conversationMode || buildingMode) && !isPending

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add {packageName} to an agent</DialogTitle>
          <DialogDescription>
            The skill is installed as a plugin the agent can invoke by name.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* A `fieldset`, not a `div role="group"`: the picker is a set of
              buttons rather than one labelled control, and the semantic
              element is what `Label` above it can name. */}
          <fieldset className="space-y-1.5">
            <Label asChild>
              <legend>Agent</legend>
            </Label>
            {isAgentsError ? (
              // Before the empty-state test, never after it: a failed read
              // that falls through to "you don't have an agent" sends someone
              // to create one they already own (R10).
              <QueryErrorAlert
                error={null}
                fallback="Couldn't load your agents"
                onRetry={() => refetchAgents()}
                compact
              />
            ) : isLoadingAgents ? (
              <p className="text-sm text-muted-foreground">Loading agents…</p>
            ) : agents.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                You don't have an agent to add this to.{" "}
                <Link to="/agents" className="text-primary hover:underline">
                  Create one
                </Link>
                .
              </p>
            ) : (
              <AgentSelectorList
                // One agent is being installed into, so the field is a trigger
                // showing that agent's badge and the picking happens in a
                // popover hanging off it — the rest of the form keeps the
                // height, and this dialog never opens a second one.
                mode="single"
                agents={agentOptions}
                selectedAgentId={agentId}
                onSelect={setAgentId}
                disabled={isPending}
                allowDeselect
              />
            )}
            {/* Plan §10: the existing plugin sync wakes a suspended target, so
                the copy says so rather than letting the wake be a surprise. */}
            <p className="text-xs text-muted-foreground">
              A suspended agent is woken to install the skill.
            </p>
          </fieldset>

          <div className="space-y-3 pt-2 border-t">
            <Label className="text-sm font-medium">Enable for:</Label>
            <div className="space-y-3">
              <div className="flex items-start space-x-3">
                <Checkbox
                  id="add-skill-conversation"
                  checked={conversationMode}
                  disabled={isPending}
                  onCheckedChange={(checked) =>
                    setConversationMode(checked === true)
                  }
                />
                <Label
                  htmlFor="add-skill-conversation"
                  className="flex items-center gap-2 font-normal cursor-pointer"
                >
                  <MessageCircle className="h-4 w-4 text-muted-foreground" />
                  Conversation mode
                </Label>
              </div>
              <div className="flex items-start space-x-3">
                <Checkbox
                  id="add-skill-building"
                  checked={buildingMode}
                  disabled={isPending}
                  onCheckedChange={(checked) =>
                    setBuildingMode(checked === true)
                  }
                />
                <Label
                  htmlFor="add-skill-building"
                  className="flex items-center gap-2 font-normal cursor-pointer"
                >
                  <Wrench className="h-4 w-4 text-muted-foreground" />
                  Building mode
                </Label>
              </div>
            </div>
            {!conversationMode && !buildingMode && (
              <p className="text-xs text-destructive">
                At least one mode must be enabled
              </p>
            )}
          </div>

          {/* One disclosure, holding the one field that has a right default. */}
          <div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="px-0 text-muted-foreground"
              onClick={() => setAdvancedOpen((prev) => !prev)}
              aria-expanded={advancedOpen}
            >
              {advancedOpen ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
              Advanced
            </Button>
            {advancedOpen && (
              <div className="space-y-1.5 pt-2">
                <Label htmlFor="add-skill-revision">Revision</Label>
                <Select
                  value={revisionNumber}
                  onValueChange={setRevisionNumber}
                  disabled={isPending}
                >
                  <SelectTrigger id="add-skill-revision" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="latest">
                      {latestLabel ? `Latest (${latestLabel})` : "Latest"}
                    </SelectItem>
                    {revisions.map((rev) => (
                      <SelectItem
                        key={rev.id}
                        value={String(rev.revision_number)}
                      >
                        {skillRevisionLabel(rev)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>

          {installMutation.isError && (
            <SkillCatalogErrorAlert
              error={installMutation.error}
              fallback="Couldn't add the skill"
            />
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            type="button"
            loading={isPending}
            disabled={!canSubmit}
            onClick={() => installMutation.mutate()}
          >
            Add to agent
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
