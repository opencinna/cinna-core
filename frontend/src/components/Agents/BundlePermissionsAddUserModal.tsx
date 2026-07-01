import { useQueryClient } from "@tanstack/react-query"
import { Plus, X } from "lucide-react"
import { useState } from "react"

import type { BundlePermissionProducer } from "@/client"
import { AgentApiService, BundlesService } from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import useCustomToast from "@/hooks/useCustomToast"

/** The fixed user when the modal opens in edit mode (user cannot change). */
export interface FixedUserContext {
  userId: string
  email?: string | null
  fullName?: string | null
}

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
  /** When set, the modal edits this fixed user (no picker, blocks prefilled). */
  fixedUser?: FixedUserContext | null
}

type ScopesByProducer = Record<string, string[]>

interface PlannedAction {
  key: string
  /** Producer agent id when this is a producer section (for cache invalidation). */
  producerAgentId?: string
  run: () => Promise<unknown>
}

const sameScopes = (a: string[], b: string[]): boolean => {
  if (a.length !== b.length) return false
  const sa = new Set(a)
  return b.every((s) => sa.has(s))
}

const extractError = (e: any): string =>
  e?.body?.detail || e?.message || "Request failed"

/**
 * Add a user (or edit a fixed user) across two independent authority domains in
 * one modal: bundle catalog access + per-manageable-producer capability scopes.
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
  fixedUser,
}: BundlePermissionsAddUserModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const isEdit = !!fixedUser
  const manageableProducers = producers.filter((p) => p.can_manage)

  const [selectedUser, setSelectedUser] =
    useState<UserAllowlistSelectedItem | null>(null)
  // The picked user's email — kept distinct from the pill's display label so a
  // future change to ``fallbackLabel`` can't silently break bundle grants.
  const [selectedEmail, setSelectedEmail] = useState<string | null>(null)
  const [scopesByProducer, setScopesByProducer] = useState<ScopesByProducer>({})
  const [sectionErrors, setSectionErrors] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)

  const existingGrantFor = (producer: BundlePermissionProducer, userId: string) =>
    producer.grants?.find((g) => g.user_id === userId)

  // Reset local state whenever the dialog (re)opens. The key tracks ``open`` too
  // so a close→reopen with the SAME fixed user still re-fires the reset.
  const resetKey = `${open}:${fixedUser?.userId ?? "add"}`
  const [lastResetKey, setLastResetKey] = useState("")
  if (resetKey !== lastResetKey) {
    setLastResetKey(resetKey)
    if (open) {
      setSelectedUser(null)
      setSelectedEmail(null)
      setSectionErrors({})
      setSubmitting(false)
      const seeded: ScopesByProducer = {}
      for (const p of manageableProducers) {
        const existing = isEdit
          ? existingGrantFor(p, fixedUser!.userId)
          : undefined
        seeded[p.producer_agent_id] = existing ? [...(existing.scopes ?? [])] : []
      }
      setScopesByProducer(seeded)
    }
  }

  const setProducerScopes = (producerAgentId: string, scopes: string[]) =>
    setScopesByProducer((prev) => ({ ...prev, [producerAgentId]: scopes }))

  const buildActions = (userId: string, email?: string | null): PlannedAction[] => {
    const actions: PlannedAction[] = []

    // Bundle access — in ADD mode, adding a user grants bundle access (no
    // opt-out). In EDIT mode we never touch bundle access: it was granted when
    // the row was created, and revocation now happens only via the row-level
    // remove on the card, not per-field toggling here.
    if (!isEdit && bundleAccessApplicable && bundleUuid && email) {
      actions.push({
        key: "bundle",
        run: () =>
          BundlesService.addGrant({
            bundleUuid,
            requestBody: { email },
          }),
      })
    }

    // Per-manageable-producer scope sections.
    for (const p of manageableProducers) {
      const desired = scopesByProducer[p.producer_agent_id] ?? []
      const existing = existingGrantFor(p, userId)
      const key = `producer:${p.producer_agent_id}`
      if (!existing) {
        if (desired.length > 0) {
          actions.push({
            key,
            producerAgentId: p.producer_agent_id,
            run: () =>
              AgentApiService.createAgentApiGrant({
                agentId: p.producer_agent_id,
                requestBody: { user_id: userId, scopes: desired },
              }),
          })
        }
      } else if (!sameScopes(existing.scopes ?? [], desired)) {
        actions.push({
          key,
          producerAgentId: p.producer_agent_id,
          run: () =>
            AgentApiService.updateAgentApiGrant({
              agentId: p.producer_agent_id,
              grantId: existing.grant_id,
              requestBody: { scopes: desired },
            }),
        })
      }
    }
    return actions
  }

  const effectiveUserId = isEdit ? fixedUser?.userId : selectedUser?.userId
  const effectiveEmail = isEdit ? fixedUser?.email : selectedEmail
  const plannedActions = effectiveUserId
    ? buildActions(effectiveUserId, effectiveEmail)
    : []
  const canSubmit =
    !submitting &&
    (isEdit || !!selectedUser) &&
    plannedActions.length > 0

  const handleSubmit = async () => {
    const userId = effectiveUserId
    if (!userId) return
    const actions = buildActions(userId, effectiveEmail)
    if (actions.length === 0) return

    setSubmitting(true)
    setSectionErrors({})

    const errors: Record<string, string> = {}
    let bundleTouched = false
    const touchedProducerIds = new Set<string>()

    for (const action of actions) {
      try {
        await action.run()
        if (action.key === "bundle") bundleTouched = true
        if (action.producerAgentId) touchedProducerIds.add(action.producerAgentId)
      } catch (e) {
        errors[action.key] = extractError(e)
      }
    }

    // The overview re-query is the source of truth after any persisted change.
    queryClient.invalidateQueries({
      queryKey: ["bundlePermissionsOverview", agentId],
    })
    if (bundleTouched && bundleUuid) {
      queryClient.invalidateQueries({
        queryKey: ["bundles", bundleUuid, "grants"],
      })
    }
    for (const producerId of touchedProducerIds) {
      queryClient.invalidateQueries({ queryKey: ["agentApiGrants", producerId] })
    }

    setSubmitting(false)

    const failedCount = Object.keys(errors).length
    if (failedCount === 0) {
      showSuccessToast(isEdit ? "Permissions updated" : "User added")
      onOpenChange(false)
    } else {
      setSectionErrors(errors)
      showErrorToast(
        `Saved with ${failedCount} issue${failedCount === 1 ? "" : "s"} — see details`,
      )
    }
  }

  const fixedLabel =
    fixedUser?.fullName && fixedUser?.email
      ? `${fixedUser.fullName} (${fixedUser.email})`
      : fixedUser?.fullName || fixedUser?.email || fixedUser?.userId

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit permissions" : "Add user"}</DialogTitle>
          <DialogDescription>
            {isEdit ? (
              <>
                Manage producer scopes for{" "}
                <span className="font-medium text-foreground">{fixedLabel}</span>.
              </>
            ) : bundleAccessApplicable ? (
              "Adding a user grants them access to this bundle. Optionally assign producer scopes in the same step."
            ) : (
              "Pick a user and assign their producer scopes."
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-1 max-h-[60vh] overflow-y-auto">
          {!isEdit && (
            <UserAllowlistPicker
              enabled={open}
              selected={selectedUser ? [selectedUser] : []}
              excludeUserIds={excludeUserIds}
              label={<Label className="text-xs text-muted-foreground">User</Label>}
              searchPlaceholder="Search users to add..."
              onAdd={(u) => {
                setSelectedUser({
                  id: u.id,
                  userId: u.id,
                  fallbackLabel: u.full_name || u.email,
                })
                setSelectedEmail(u.email)
              }}
              onRemove={() => {
                setSelectedUser(null)
                setSelectedEmail(null)
              }}
            />
          )}

          {!isEdit && bundleAccessApplicable && sectionErrors["bundle"] && (
            <p className="text-xs text-destructive">
              Bundle access: {sectionErrors["bundle"]}
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
            {submitting ? "Saving..." : isEdit ? "Save changes" : "Add user"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

interface ProducerScopeBlockProps {
  producer: BundlePermissionProducer
  scopes: string[]
  onChange: (scopes: string[]) => void
  error?: string
}

/**
 * Scope multiselect for one manageable producer: removable assigned chips,
 * catalog quick-add chips, and a free-text add — lifted from the GrantDialog
 * body in ``AgentApiAccessScopesCard.tsx`` so the two surfaces feel identical.
 */
function ProducerScopeBlock({
  producer,
  scopes,
  onChange,
  error,
}: ProducerScopeBlockProps) {
  const [customScope, setCustomScope] = useState("")
  const catalog = producer.scope_catalog ?? []
  const unassignedCatalog = catalog.filter((s) => !scopes.includes(s.name))

  const toggleScope = (scope: string) =>
    onChange(
      scopes.includes(scope)
        ? scopes.filter((s) => s !== scope)
        : [...scopes, scope],
    )

  const addCustomScope = () => {
    const value = customScope.trim()
    if (value && !scopes.includes(value)) onChange([...scopes, value])
    setCustomScope("")
  }

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex items-center gap-2">
        <AgentBadge
          agent={{
            id: producer.producer_agent_id,
            name: producer.producer_agent_name || "Producer",
            ui_color_preset: producer.producer_ui_color_preset,
          }}
        />
        <span className="text-xs text-muted-foreground">scopes</span>
      </div>

      {/* Assigned scopes (removable chips). */}
      <div className="flex flex-wrap gap-1.5">
        {scopes.length === 0 ? (
          <span className="text-xs text-muted-foreground italic">
            No scopes — identified but no capabilities granted.
          </span>
        ) : (
          scopes.map((scope) => (
            <Badge key={scope} variant="secondary" className="gap-1 text-xs">
              {scope}
              <button
                type="button"
                onClick={() => toggleScope(scope)}
                className="hover:text-destructive transition-colors"
                aria-label={`Remove scope ${scope}`}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))
        )}
      </div>

      {/* Quick-add from the producer's policy.yaml catalog. */}
      {unassignedCatalog.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {unassignedCatalog.map((s) => (
            <button
              key={s.name}
              type="button"
              onClick={() => toggleScope(s.name)}
              title={s.description ?? undefined}
              className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent transition-colors"
            >
              <Plus className="h-3 w-3" />
              {s.name}
            </button>
          ))}
        </div>
      )}

      {/* Free-text scope add (catalog may be empty). */}
      <div className="flex items-center gap-1.5">
        <Input
          value={customScope}
          onChange={(e) => setCustomScope(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault()
              addCustomScope()
            }
          }}
          placeholder="Add a scope name..."
          className="h-8 text-xs"
        />
        <Button
          variant="outline"
          size="sm"
          className="h-8 shrink-0"
          onClick={addCustomScope}
          disabled={!customScope.trim()}
        >
          Add
        </Button>
      </div>

      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  )
}
