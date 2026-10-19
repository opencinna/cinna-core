import type { ReactNode } from "react"

import type { UserGroupImpact } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import { pluralize } from "@/utils/pluralize"
import { sharesByTypeSentence } from "@/utils/userGroupShareTypes"

export type ImpactFact = [label: string, value: string]

/**
 * "2 cleared, 1 of them restarted": the restarted environments are a subset
 * of the cleared ones, so a " · " pair would read as 3. Empty when 0.
 */
function aiEnvironmentsValue(impact: UserGroupImpact): string {
  const cleared = impact.ai_environments_cleared ?? 0
  const restarted = impact.ai_environments_restarted ?? 0
  if (cleared === 0) return ""
  return restarted > 0
    ? `${cleared} cleared, ${restarted} of them restarted`
    : `${cleared} cleared`
}

/**
 * The impact as noun-labelled facts, each value carrying its unit and each
 * row left out when its value is 0 (§2 Fact labels, Absent facts). The three
 * confirms pick which of the facts they show.
 */
export function impactFacts(
  impact: UserGroupImpact,
  include: {
    members?: string
    shares?: string
    peopleLosing?: string
    links?: boolean
    aiEnvironments?: boolean
  },
): ImpactFact[] {
  const facts: ImpactFact[] = []
  const members = impact.member_count ?? 0
  if (include.members && members > 0) {
    facts.push([include.members, pluralize(members, "person", "people")])
  }
  const shares = sharesByTypeSentence(impact.shares_by_type)
  if (include.shares && shares) facts.push([include.shares, shares])
  const losing = impact.users_losing_credential_access ?? 0
  if (include.peopleLosing && losing > 0) {
    facts.push([include.peopleLosing, pluralize(losing, "person", "people")])
  }
  const links = impact.credential_links_removed ?? 0
  if (include.links && links > 0) {
    facts.push(["Credential links removed", pluralize(links, "link", "links")])
  }
  const environments = aiEnvironmentsValue(impact)
  if (include.aiEnvironments && environments) {
    facts.push(["AI environments", environments])
  }
  return facts
}

interface UserGroupImpactDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  /** One sentence under the title (what the action does). */
  description?: ReactNode
  /** The impact read: `undefined` while loading. */
  impact: UserGroupImpact | undefined
  impactError: unknown
  onRetryImpact: () => void
  /** Facts derived from the loaded impact; `[]` renders `noImpactSentence`. */
  facts: ImpactFact[]
  noImpactSentence: string
  /** A closing sentence ("This can't be undone."). */
  footnote?: string
  confirmLabel: string
  pending: boolean
  onConfirm: () => void
  /**
   * Where focus goes when the dialog closes. Radix returns it to the control
   * that opened the dialog, which a successful removal unmounts with its row;
   * the caller then moves it somewhere deliberate (§2 Choice-control ARIA).
   */
  onCloseAutoFocus?: (event: Event) => void
}

/**
 * The shell of every fetched-impact confirm on the User Groups pages (S4,
 * S7, S8): a plain `Dialog` — the R6 exception for a confirm that must show
 * what else breaks — naming the entity, listing the cost, and ending in one
 * destructive button that waits for the impact to load.
 */
export function UserGroupImpactDialog({
  open,
  onOpenChange,
  title,
  description,
  impact,
  impactError,
  onRetryImpact,
  facts,
  noImpactSentence,
  footnote,
  confirmLabel,
  pending,
  onConfirm,
  onCloseAutoFocus,
}: UserGroupImpactDialogProps) {
  const failed = impact === undefined && impactError != null

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!pending) onOpenChange(next)
      }}
    >
      <DialogContent
        className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0"
        onCloseAutoFocus={onCloseAutoFocus}
      >
        <DialogHeader>
          <DialogTitle className="break-words">{title}</DialogTitle>
          {description && (
            <DialogDescription className="break-words">
              {description}
            </DialogDescription>
          )}
        </DialogHeader>

        <div className="space-y-3 py-1">
          {failed ? (
            <QueryErrorAlert
              error={impactError}
              fallback="Couldn't work out who loses access"
              onRetry={onRetryImpact}
              compact
            />
          ) : impact === undefined ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          ) : facts.length > 0 ? (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
              {facts.map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="text-xs leading-5 text-muted-foreground">
                    {label}
                  </dt>
                  <dd className="text-sm break-words">{value}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="text-sm break-words">{noImpactSentence}</p>
          )}
          {footnote && (
            <p className="text-sm text-muted-foreground">{footnote}</p>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <LoadingButton
            type="button"
            variant="destructive"
            loading={pending}
            disabled={impact === undefined}
            onClick={onConfirm}
          >
            {confirmLabel}
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
