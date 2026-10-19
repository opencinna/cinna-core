import { useState } from "react"

import type { BundlePermissionProducer } from "@/client"
import { BundlePermissionsTargetPicker } from "@/components/Agents/BundlePermissions/BundlePermissionsTargetPicker"
import {
  buildActions,
  type PermissionTarget,
  type ScopesByProducer,
  scopeGrantFor,
} from "@/components/Agents/BundlePermissions/bundlePermissionsActions"
import { ProducerScopeBlock } from "@/components/Agents/BundlePermissions/ProducerScopeBlock"
import { useBundlePermissionsSave } from "@/components/Agents/BundlePermissions/useBundlePermissionsSave"
import { SCOPE_EMPTY_HINTS } from "@/components/Common/AgentApiScopeEditor"
import type {
  GroupSelectedItem,
  UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

interface BundlePermissionsAddUserModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  agentId: string
  bundleUuid: string | null
  bundleAccessApplicable: boolean
  /** All producers from the overview; only ``can_manage`` ones are editable. */
  producers: BundlePermissionProducer[]
  /** User ids already in the table — filtered out of the add-mode picker. */
  excludeUserIds: string[]
  /** Group ids already in the table — not offered again. */
  excludeGroupIds?: string[]
  /**
   * When set, the modal edits this fixed person or group (no picker, blocks
   * prefilled).
   */
  fixedTarget?: PermissionTarget | null
}

/** "Full Name (email)" for a person, the name for a group. */
function targetLabel(target: PermissionTarget): string {
  if (target.kind === "group") return target.name
  if (target.fullName && target.email)
    return `${target.fullName} (${target.email})`
  return target.fullName || target.email || target.id
}

/**
 * Add a person or a group (or edit a fixed one) across two independent
 * authority domains in one modal: bundle catalog access + per-manageable-
 * producer capability scopes. A group's scopes add to each active member's
 * own.
 *
 * Submit fans out to the existing, already owner-gated endpoints sequentially,
 * accumulating a per-section result. Successful sections persist even when a
 * later section fails (no cross-domain transaction); failed sections render an
 * inline error and the overview re-query is the source of truth on close
 * (Decision 2). Submit is disabled until at least one net change is staged
 * (Decision 3).
 */
export function BundlePermissionsAddUserModal({
  open,
  onOpenChange,
  agentId,
  bundleUuid,
  bundleAccessApplicable,
  producers,
  excludeUserIds,
  excludeGroupIds,
  fixedTarget,
}: BundlePermissionsAddUserModalProps) {
  const isEdit = !!fixedTarget
  const manageableProducers = producers.filter((p) => p.can_manage)

  const [selectedUser, setSelectedUser] =
    useState<UserAllowlistSelectedItem | null>(null)
  // The picked user's email — kept distinct from the pill's display label so a
  // future change to ``fallbackLabel`` can't silently break bundle grants.
  const [selectedEmail, setSelectedEmail] = useState<string | null>(null)
  // A group instead of a person. Picking one clears the other.
  const [selectedGroup, setSelectedGroup] = useState<GroupSelectedItem | null>(
    null,
  )
  const groupsOffered =
    !isEdit && (bundleAccessApplicable || manageableProducers.length > 0)
  const [scopesByProducer, setScopesByProducer] = useState<ScopesByProducer>({})
  const { submitting, sectionErrors, resetSaveState, submitActions } =
    useBundlePermissionsSave({ agentId, bundleUuid, onOpenChange })

  // Reset local state whenever the dialog (re)opens. The key tracks ``open`` too
  // so a close→reopen with the SAME fixed target still re-fires the reset.
  const resetKey = `${open}:${fixedTarget ? `${fixedTarget.kind}:${fixedTarget.id}` : "add"}`
  const [lastResetKey, setLastResetKey] = useState("")
  if (resetKey !== lastResetKey) {
    setLastResetKey(resetKey)
    if (open) {
      setSelectedUser(null)
      setSelectedEmail(null)
      setSelectedGroup(null)
      resetSaveState()
      const seeded: ScopesByProducer = {}
      for (const p of manageableProducers) {
        const existing = fixedTarget ? scopeGrantFor(p, fixedTarget) : undefined
        seeded[p.producer_agent_id] = existing
          ? [...(existing.scopes ?? [])]
          : []
      }
      setScopesByProducer(seeded)
    }
  }

  const setProducerScopes = (producerAgentId: string, scopes: string[]) =>
    setScopesByProducer((prev) => ({ ...prev, [producerAgentId]: scopes }))

  const target: PermissionTarget | null = fixedTarget
    ? fixedTarget
    : selectedGroup
      ? { kind: "group", id: selectedGroup.groupId, name: selectedGroup.name }
      : selectedUser
        ? { kind: "user", id: selectedUser.userId, email: selectedEmail }
        : null

  const plannedActions = target
    ? buildActions({
        target,
        isEdit,
        agentId,
        bundleAccessApplicable,
        bundleUuid,
        manageableProducers,
        scopesByProducer,
      })
    : []
  const canSubmit = !submitting && plannedActions.length > 0

  // An empty list means different things: a person's or group's existing
  // grant still identifies them; with no grant, saving nothing writes nothing.
  const emptyHintFor = (producer: BundlePermissionProducer) => {
    if (!target || !scopeGrantFor(producer, target))
      return SCOPE_EMPTY_HINTS.none
    return target.kind === "group"
      ? SCOPE_EMPTY_HINTS.group
      : SCOPE_EMPTY_HINTS.person
  }

  const handleSubmit = async () => {
    if (!target) return
    await submitActions(
      plannedActions,
      isEdit
        ? "Permissions updated"
        : target.kind === "group"
          ? `${target.name} added`
          : "User added",
      target.kind === "group",
    )
  }

  const description = fixedTarget ? (
    <>
      Manage producer scopes for{" "}
      <span className="font-medium text-foreground">
        {targetLabel(fixedTarget)}
      </span>
      .
      {fixedTarget.kind === "group" && " They add to each member's own scopes."}
    </>
  ) : !groupsOffered ? (
    "Pick a user and assign their producer scopes."
  ) : bundleAccessApplicable ? (
    "Adding a person or a group grants them access to this bundle. Optionally assign producer scopes in the same step; a group's scopes add to each member's own."
  ) : (
    "Pick a person or a group and assign their producer scopes. A group's scopes add to each member's own."
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-lg [&>*]:min-w-0"
        // Radix would focus the first focusable control, which in edit mode is
        // a scope chip's remove X — Enter would then drop a scope. Start on
        // the first text field instead (the picker's search, or the first
        // producer's "Add a scope name" input).
        onOpenAutoFocus={(e) => {
          const field = (
            e.currentTarget as HTMLElement
          ).querySelector<HTMLInputElement>("input:not([disabled])")
          if (field) {
            e.preventDefault()
            field.focus()
          }
        }}
      >
        <DialogHeader>
          <DialogTitle>
            {isEdit
              ? "Edit permissions"
              : groupsOffered
                ? "Add access"
                : "Add user"}
          </DialogTitle>
          <DialogDescription className="break-words">
            {description}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-1 -mr-2 pr-2 max-h-[60vh] overflow-y-auto">
          {!isEdit && (
            <BundlePermissionsTargetPicker
              enabled={open}
              groupsOffered={groupsOffered}
              excludeUserIds={excludeUserIds}
              excludeGroupIds={excludeGroupIds}
              selectedUser={selectedUser}
              selectedGroup={selectedGroup}
              onUserChange={(user, email) => {
                setSelectedUser(user)
                setSelectedEmail(email)
              }}
              onGroupChange={setSelectedGroup}
            />
          )}

          {!isEdit && bundleAccessApplicable && sectionErrors.bundle && (
            <p className="text-xs text-destructive">
              Bundle access: {sectionErrors.bundle}
            </p>
          )}

          {manageableProducers.map((producer) => (
            <ProducerScopeBlock
              key={producer.producer_agent_id}
              producer={producer}
              scopes={scopesByProducer[producer.producer_agent_id] ?? []}
              onChange={(next) =>
                setProducerScopes(producer.producer_agent_id, next)
              }
              emptyHint={emptyHintFor(producer)}
              error={sectionErrors[`producer:${producer.producer_agent_id}`]}
            />
          ))}

          {manageableProducers.length === 0 &&
            (isEdit || !bundleAccessApplicable) && (
              <p className="text-xs text-muted-foreground">
                No producer scopes to assign — there is no producer you can
                manage here.
              </p>
            )}

          {Object.keys(sectionErrors).length > 0 && (
            <p className="text-xs text-muted-foreground">
              Sections without an error were saved. Close and reopen to see the
              current state.
            </p>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            {submitting
              ? "Saving..."
              : isEdit
                ? "Save changes"
                : selectedGroup
                  ? "Add group"
                  : "Add user"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
