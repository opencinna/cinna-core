/**
 * Confirm for unsharing a credential from one person, opened only when that
 * person has something that stops working: a cloud agent linking it, or one
 * of the owner's skills installed on their own computer. A plain unshare
 * stays one click (the picker pill's remove).
 *
 * A `Dialog` rather than an `AlertDialog` because it carries fetched usage
 * rows (§2 Confirmation). The owner may proceed; core does not block it.
 */
import { AlertTriangle } from "lucide-react"

import type { CredentialSharePublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import {
  RecipientAgentRow,
  RecipientLocalSkillRow,
} from "./CredentialUsageRows"

/** Whether unsharing from this person breaks anything of theirs. */
export function shareHasUsages(share: CredentialSharePublic): boolean {
  return (
    (share.agent_usages?.length ?? 0) > 0 ||
    (share.direct_agent_use_count ?? 0) > 0 ||
    (share.local_skill_usages?.length ?? 0) > 0
  )
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

/** "2 agents and 1 skill on their computer" — only the non-empty halves. */
function usageSummary(agents: number, localSkills: number): string {
  const parts: string[] = []
  if (agents > 0) parts.push(plural(agents, "agent", "agents"))
  if (localSkills > 0)
    parts.push(`${plural(localSkills, "skill", "skills")} on their computer`)
  return parts.join(" and ")
}

interface UnshareCredentialDialogProps {
  credentialName: string
  /** The share being removed; `null` closes the dialog. */
  share: CredentialSharePublic | null
  isPending: boolean
  onConfirm: (share: CredentialSharePublic) => void
  onClose: () => void
}

export function UnshareCredentialDialog({
  credentialName,
  share,
  isPending,
  onConfirm,
  onClose,
}: UnshareCredentialDialogProps) {
  const agentUsages = share?.agent_usages ?? []
  const localSkillUsages = share?.local_skill_usages ?? []
  // Agents that link it without any of the owner's skills: counted, not named.
  const directAgentCount = share?.direct_agent_use_count ?? 0
  const person = share?.shared_with_email || "This person"
  // One skill installed in two agents on their computer is still one skill.
  const localSkillCount = new Set(localSkillUsages.map((u) => u.package_uuid))
    .size
  const hasRows = agentUsages.length > 0 || localSkillUsages.length > 0

  return (
    <Dialog
      open={share !== null}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!next && !isPending) onClose()
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">
            Stop sharing {credentialName}?
          </DialogTitle>
          <DialogDescription className="break-words">
            {person} loses access to this credential right away.
          </DialogDescription>
        </DialogHeader>
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Some of their work stops working</AlertTitle>
          <AlertDescription className="break-words">
            {person} uses it in{" "}
            {usageSummary(
              agentUsages.length + directAgentCount,
              localSkillCount,
            )}
            . They will be without it until you share it with them again.
          </AlertDescription>
        </Alert>
        {hasRows && (
          <ListRowGroup>
            {agentUsages.map((usage) => (
              <RecipientAgentRow key={usage.id} usage={usage} />
            ))}
            {localSkillUsages.map((usage, index) => (
              <RecipientLocalSkillRow
                // One skill may sit in several agents on their computer, so the
                // position disambiguates.
                key={`${usage.package_uuid}-${index}`}
                usage={usage}
              />
            ))}
          </ListRowGroup>
        )}
        {/* With no named rows the alert's count already says it all. */}
        {hasRows && directAgentCount > 0 && (
          <p className="text-sm text-muted-foreground">
            {directAgentCount}
            {agentUsages.length > 0 ? " other" : ""}{" "}
            {directAgentCount === 1 ? "agent uses" : "agents use"} it directly.
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <LoadingButton
            variant="destructive"
            loading={isPending}
            onClick={() => share && onConfirm(share)}
          >
            Stop sharing
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
