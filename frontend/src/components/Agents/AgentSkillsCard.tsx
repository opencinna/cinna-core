import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { formatDistanceToNow } from "date-fns"
import { GraduationCap, Info, Loader2, RefreshCw } from "lucide-react"
import { useMemo, useState } from "react"

import type { SkillEntryPublic } from "@/client"
import { AgentsService } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
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
import { skillKey, skillsIndexErrorCopy, sortSkills } from "@/utils/skills"
import { AllSkillsSheet } from "./AllSkillsSheet"
import { SkillRow } from "./SkillRow"

/** Stable identity so the sort memoises while the query is in flight. */
const NO_SKILLS: SkillEntryPublic[] = []

interface AgentSkillsCardProps {
  agentId: string
}

/**
 * "Skills" — the folders under `skills/` this agent can invoke by name.
 *
 * A View surface (guidelines §1): the card answers "which skills are there,
 * can the engine actually see them, and what does one say" at a glance. The
 * only mutation is the header's Refresh, which re-reads the environment; skills
 * are authored in the workspace, not in the web UI, so the header carries no
 * Create action.
 *
 * Rendered for consumers of a foreign install too — knowing what an installed
 * agent can do is a use capability. The row's `⋯` menu (Publish to catalog…) is
 * gated on the response-level and entry-level `can_publish` the server sends,
 * never on a client-side role check: a foreign install is use-only for every
 * role, and only the server knows that.
 */
export function AgentSkillsCard({ agentId }: AgentSkillsCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isSheetOpen, setIsSheetOpen] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["agent", agentId, "skills"],
    queryFn: () => AgentsService.getAgentSkills({ agentId }),
    enabled: !!agentId,
  })

  const refreshMutation = useMutation({
    mutationFn: () => AgentsService.refreshAgentSkills({ agentId }),
    onSuccess: (result) => {
      // A 200 is not the same as a successful re-read: the route deliberately
      // never fails on an unreachable environment, it answers with the cached
      // rows plus an `error` code. Toasting "Skills refreshed" over that would
      // tell the user the opposite of what the banner below is about to say.
      if (result.error) {
        showErrorToast("Couldn't re-read the skills")
      } else {
        showSuccessToast("Skills refreshed")
      }
      // Prefix invalidation: the index and every open SKILL.md body were both
      // read from the environment we just re-read.
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "skills"] })
    },
    onError: handleError.bind(showErrorToast),
  })

  const skills = data?.skills ?? NO_SKILLS
  const sortedSkills = useMemo(() => sortSkills(skills), [skills])

  // The environment could not be read. It is not a request failure — the
  // payload arrived and says why — so it renders as an `Alert` above whatever
  // rows the cache still holds, not as an error state that hides them.
  const indexError = data?.error ? skillsIndexErrorCopy(data.error) : null

  // When the payload carries an error *and* no cached rows, the Alert has
  // already said what happened; printing "No skills yet" underneath it would
  // assert something the server did not.
  const alertReplacesEmpty =
    !!indexError && !isLoading && sortedSkills.length === 0

  // An agent with no environment has never had its `skills/` folder read, so
  // "No skills yet" would assert something the server did not say — and it
  // would contradict the Refresh tooltip, which correctly reads "Not checked
  // yet". Two states, two sentences.
  const emptyState =
    data && !data.environment_id ? (
      <p className="text-sm text-muted-foreground">
        This agent has no environment yet, so there is nothing to read skills
        from.
      </p>
    ) : (
      // One sentence, and the page it names is a link: skills cannot be
      // created in the web UI at all, so the card's own Refresh is not the
      // action this state should point at (§2 "Empty state"). What a skill
      // *is* stays in the description above rather than being repeated here.
      <p className="text-sm text-muted-foreground">
        No skills yet — the agent creates them while it builds, or you add one
        from your{" "}
        <Link
          to="/settings"
          hash="security"
          className="text-primary hover:underline"
        >
          local kit
        </Link>
        .
      </p>
    )

  const checkedAt = data?.fetched_at
  let checkedLabel = "Not checked yet"
  if (checkedAt) {
    try {
      checkedLabel = `Checked ${formatDistanceToNow(new Date(checkedAt), {
        addSuffix: true,
      })}`
    } catch {
      checkedLabel = "Checked at an unknown time"
    }
  }

  return (
    <Card>
      <CardHeader>
        {/* Title and action share one row; the description spans the full
            header width below them, so it stays two lines at 1024 instead of
            being squeezed into a column beside the button. */}
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <GraduationCap className="h-5 w-5 shrink-0" />
            Skills
          </CardTitle>
          <div className="shrink-0">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => refreshMutation.mutate()}
                  disabled={refreshMutation.isPending}
                >
                  {refreshMutation.isPending ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCw className="mr-2 h-4 w-4" />
                  )}
                  Refresh
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                {checkedLabel}
              </TooltipContent>
            </Tooltip>
          </div>
        </div>
        <CardDescription>
          Skills are folders under <code>skills/</code> the agent can invoke by
          name; the model sees only the name and description until it uses one.
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
            keeps the rows on screen instead of blanking the card — a first
            failed load still renders as a failure, never as "no skills yet". */}
        {!alertReplacesEmpty && (
          <PreviewList
            items={sortedSkills}
            getKey={skillKey}
            renderItem={(skill) => (
              <SkillRow
                agentId={agentId}
                skill={skill}
                canPublish={data?.can_publish}
              />
            )}
            isLoading={isLoading}
            isError={isError && !data}
            error={error}
            onRetry={() => refetch()}
            errorFallback="Couldn't load skills"
            empty={emptyState}
            onShowAll={() => setIsSheetOpen(true)}
          />
        )}
      </CardContent>

      <AllSkillsSheet
        agentId={agentId}
        skills={sortedSkills}
        canPublish={data?.can_publish}
        open={isSheetOpen}
        onOpenChange={setIsSheetOpen}
      />
    </Card>
  )
}
