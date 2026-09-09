import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Blocks, Info, Loader2, Plus, RefreshCw } from "lucide-react"
import { useState } from "react"

import type { AddonPublic } from "@/client"
import { AgentsService } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { addonsQueryKey, invalidateAddons } from "@/utils/addons"
import { skillsIndexErrorCopy } from "@/utils/skills"
import { AddAddonDialog } from "./AddAddonDialog"
import { AddonRow, type SyncReporter } from "./AddonRow"
import { AllAddonsSheet } from "./AllAddonsSheet"

/** Stable identity so the list memoises while the query is in flight. */
const NO_ADDONS: AddonPublic[] = []

interface AddonsCardProps {
  agentId: string
  onSyncResult: SyncReporter
}

/**
 * "Everything this agent can do beyond its prompt" — S1.
 *
 * **One list, not two.** The agent page used to answer this question twice —
 * the Plugins tab listed links, the Configuration tab's Skills card listed the
 * environment's index, and a catalog install appeared in both. `AddonsService`
 * folds them server-side and sorts the result attention-first, explicitly
 * refusing kind as a sort key: a broken plugin and a broken skill are equally
 * urgent, and grouping by kind would push one of them off the preview. Two
 * capped cards here would reintroduce exactly that. The umbrella lives on the
 * tab, the distinction lives on the row (plan §10: rows say plugin or skill,
 * never "addon"), and the counts live in this description.
 *
 * A P5 preview list: five rows and a "Show all (N)" Sheet, house rows, one
 * interaction model — the mode toggle auto-saves, everything else is a menu
 * item that opens a confirm or a dialog.
 *
 * Full tab width, like the card it replaces: the Addons tab is a single-column
 * `space-y-6` stack, not a card grid, so A8 — which is about a card spanning
 * two columns of a grid — does not apply.
 */
export function AddonsCard({ agentId, onSyncResult }: AddonsCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [sheetOpen, setSheetOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: addonsQueryKey(agentId),
    queryFn: () => AgentsService.getAgentAddons({ agentId }),
    enabled: !!agentId,
  })

  const refreshMutation = useMutation({
    mutationFn: () => AgentsService.refreshAgentAddons({ agentId }),
    onSuccess: (result) => {
      // A 200 is not the same as a successful re-read: the route deliberately
      // never fails on an unreachable environment, it answers with the cached
      // rows plus a `skills_error` code. Toasting success over that would tell
      // the user the opposite of what the Alert below is about to say.
      if (result.skills_error) {
        showErrorToast("Couldn't re-read this agent's skills")
      } else {
        showSuccessToast("Plugins and skills refreshed")
      }
      // The refresh re-read the environment, so the skill index every other
      // consumer holds is stale too — not just this projection.
      invalidateAddons(queryClient, agentId)
    },
    onError: handleError.bind(showErrorToast),
  })

  // Delivered already sorted — status first, then name. Do not re-sort:
  // `sortSkills` orders the skill *index*, and applying it here would put a
  // healthy plugin above a broken one because a plugin row has no `error` of
  // its own to rank on.
  const addons = data?.addons ?? NO_ADDONS
  const canAdd = !!data?.can_add
  const counts = data?.counts

  // The environment could not be read. Not a request failure — the payload
  // arrived and says why — so it renders as an `Alert` above whatever rows the
  // cache still holds. The plugin half never depends on it.
  const indexError = data?.skills_error
    ? skillsIndexErrorCopy(data.skills_error)
    : null

  // When the payload carries an error *and* there is nothing to list, the
  // Alert has already said what happened; printing "Nothing installed yet"
  // underneath it would assert something the server did not.
  const alertReplacesEmpty = !!indexError && !isLoading && addons.length === 0

  const countsLine = counts
    ? [
        `${counts.plugins ?? 0} ${counts.plugins === 1 ? "plugin" : "plugins"}`,
        `${counts.skills ?? 0} ${counts.skills === 1 ? "skill" : "skills"}`,
        // A subset of `skills`, never a third bucket beside it, so it is
        // phrased as one rather than added to the other two.
        ...((counts.local_skills ?? 0) > 0
          ? [`${counts.local_skills} built here`]
          : []),
      ].join(" · ")
    : null

  const emptyState =
    data && !data.environment_id ? (
      <p className="text-sm text-muted-foreground">
        This agent has no environment yet, so there is nothing to read skills
        from.
      </p>
    ) : canAdd ? (
      // The header's "Add addon" is this state's action, which is what §2
      // asks an empty state to point at.
      <p className="text-sm text-muted-foreground">
        Nothing installed yet — add a plugin or a skill from the catalog.
      </p>
    ) : (
      <p className="text-sm text-muted-foreground">
        This agent carries no plugins or skills.
      </p>
    )

  const checkedAt = data?.fetched_at
  const checkedAgo = checkedAt ? formatRelativeTimestamp(checkedAt) : null
  const checkedLabel = !checkedAt
    ? "Not checked yet"
    : checkedAgo
      ? `Checked ${checkedAgo}`
      : "Checked at an unknown time"

  return (
    <Card>
      <CardHeader className="pb-3">
        {/* Title and the two controls share one row; the description spans the
            full header width below them, so it stays readable at 1024 instead
            of being squeezed into a column beside a button. */}
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex min-w-0 items-center gap-2">
            <Blocks className="h-5 w-5 shrink-0" />
            {/* Not "Addons": the tab is already called that, and a card that
                repeats its tab's name names nothing. */}
            Plugins and skills
          </CardTitle>
          <div className="flex shrink-0 items-center gap-2">
            {canAdd && (
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="mr-2 h-4 w-4" />
                Add addon
              </Button>
            )}
            {/* Deliberately **not** gated on `can_add`: re-reading what an
                agent carries is a use capability, the server gates this route
                on access rather than on the developer role, and a consumer of
                a foreign install left with a stale index and no way to refresh
                it is the same capability regression this tab exists to avoid. */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  aria-label="Re-read this agent's plugins and skills"
                  disabled={refreshMutation.isPending}
                  onClick={() => refreshMutation.mutate()}
                >
                  {refreshMutation.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                {checkedLabel}
              </TooltipContent>
            </Tooltip>
          </div>
        </div>
        <CardDescription>
          Everything this agent can do beyond its prompt.
          {countsLine && <> {countsLine}.</>}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-3">
        {indexError && (
          <Alert>
            <Info />
            <AlertDescription>{indexError}</AlertDescription>
          </Alert>
        )}

        {/* The cap, the "Show all" link and the four states are the shared P5
            primitive's job; the row and the copy stay here. `isError` is gated
            on there being no data to show, so a failed *background* refetch
            keeps the rows on screen — a first failed load still renders as a
            failure, never as "nothing installed yet". */}
        {!alertReplacesEmpty && (
          <PreviewList
            items={addons}
            getKey={(addon) => addon.key}
            renderItem={(addon) => (
              <AddonRow
                agentId={agentId}
                addon={addon}
                canAdd={canAdd}
                onSyncResult={onSyncResult}
              />
            )}
            isLoading={isLoading}
            isError={isError && !data}
            error={error}
            onRetry={() => refetch()}
            errorFallback="Couldn't load this agent's addons"
            empty={emptyState}
            onShowAll={() => setSheetOpen(true)}
          />
        )}
      </CardContent>

      <AllAddonsSheet
        agentId={agentId}
        addons={addons}
        canAdd={canAdd}
        onSyncResult={onSyncResult}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />

      {/* Mounted only while open: a resident dialog would run the discover and
          catalog queries as soon as the tab rendered. */}
      {addOpen && (
        <AddAddonDialog
          agentId={agentId}
          installedAddons={addons}
          addonsUnavailable={isError && !data}
          addonsError={error}
          onRetryAddons={() => refetch()}
          onSyncResult={onSyncResult}
          open
          onOpenChange={setAddOpen}
        />
      )}
    </Card>
  )
}
