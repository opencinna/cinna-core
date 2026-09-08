import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { ChevronDown, ChevronRight, MessageCircle, Wrench } from "lucide-react"
import { useMemo, useState } from "react"

import { AgentsService, SkillsService } from "@/client"
import { SearchableSelect } from "@/components/Common/SearchableSelect"
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
 * built to `Agents/InstallPluginModal`'s skeleton: the two mode `Checkbox`es
 * are literally the same control the marketplace install uses, and a skill that
 * installs differently from a plugin — when the backend makes it *one* plugin
 * link either way — would be a second vocabulary for one act.
 *
 * The agent picker is `Common/SearchableSelect`, a `Popover` anchored to the
 * field. Never `AgentSelectorDialog`: that is a `Dialog`, and a picker dialog
 * on top of a form dialog is exactly what §2 "Disclosure depth" forbids.
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

  const { data: agentsData, isLoading: isLoadingAgents } = useQuery({
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
  const agentOptions = useMemo(
    () => agents.map((agent) => ({ value: agent.id, label: agent.name })),
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
      // The Plugins tab's installed list, and the catalog's own
      // `installed_in_agent_ids` / `install_count`, both just changed.
      queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })
      queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
      // So does the agent's skill index: a catalog install shows up there as
      // `source: "catalog"` with its own row and flag. The index route is
      // cache-only, so this may return the same list until the Skills card's
      // Refresh re-reads the container — but `can_publish` and the row's flags
      // are computed per request, so it is still strictly better than leaving
      // a card that now names the wrong set.
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "skills"] })
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
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add {packageName} to an agent</DialogTitle>
          <DialogDescription>
            The skill is installed as a plugin the agent can invoke by name.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="add-skill-agent">Agent</Label>
            {isLoadingAgents ? (
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
              <SearchableSelect
                value={agentId}
                onChange={setAgentId}
                options={agentOptions}
                placeholder="Choose an agent"
                searchPlaceholder="Search agents…"
                emptyText="No agents match."
                disabled={isPending}
                className="w-full"
              />
            )}
            {/* Plan §10: the existing plugin sync wakes a suspended target, so
                the copy says so rather than letting the wake be a surprise. */}
            <p className="text-xs text-muted-foreground">
              A suspended agent is woken to install the skill.
            </p>
          </div>

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
