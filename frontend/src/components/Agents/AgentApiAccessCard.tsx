import { useMutation, useQueryClient } from "@tanstack/react-query"
import { KeyRound, PauseCircle, Plus } from "lucide-react"
import { useMemo, useState } from "react"

import type { AgentApiAccessEntry, AgentApiAccessGroupEntry } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
import { invalidateUserGroupShareLists } from "@/components/Admin/UserGroups/userGroupQueries"
import { PreviewList } from "@/components/Common/PreviewList"
import type { UserSelectValue } from "@/components/Common/UserSelectField"
import { AgentApiKeyDialog } from "@/components/Credentials/AgentApiKeyDialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  agentApiAccessQueryKey,
  useAgentApiAccess,
} from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { AllApiAccessSheet } from "./AllApiAccessSheet"
import { ApiAccessGroupRow, RemoveGroupScopesDialog } from "./ApiAccessGroupRow"
import { ApiAccessPersonDialog } from "./ApiAccessPersonDialog"
import { ApiAccessPersonRow } from "./ApiAccessPersonRow"
import {
  ApiAccessScopesDialog,
  type ApiAccessScopesTarget,
} from "./ApiAccessScopesDialog"
import { ApiAccessSwitchLine } from "./ApiAccessSwitchLine"
import {
  apiAccessPersonLabel,
  sortApiAccessEntries,
  summarizeApiAccessPerson,
} from "./apiAccess"

interface AgentApiAccessCardProps {
  agentId: string
  agentName: string
  /** Agent-level values, used until the access list (which carries both) loads. */
  externalAccessEnabled: boolean
  identityEnabled: boolean
}

const NO_ENTRIES: AgentApiAccessEntry[] = []
const NO_GROUPS: AgentApiAccessGroupEntry[] = []

function asRecipient(entry: AgentApiAccessEntry): UserSelectValue {
  return { userId: entry.user.id, label: apiAccessPersonLabel(entry) }
}

/**
 * "API access" — who can call this agent's REST API from outside with a key,
 * with what scopes, and the two producer switches that govern all of it.
 *
 * One card replacing the old External keys and Access & Scopes sub-cards of
 * the Agent REST API card (plan D11): keys and scopes are two facts about the
 * same person, so they are one row per person. User groups holding scopes
 * follow under their own heading (scopes only — a group has no keys). The
 * switches auto-save; every row verb opens its own dialog (same split as the
 * Channels card).
 *
 * Rendered by the host only for the owner and only while the REST API is on.
 */
export function AgentApiAccessCard({
  agentId,
  agentName,
  externalAccessEnabled,
  identityEnabled,
}: AgentApiAccessCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [sheetOpen, setSheetOpen] = useState(false)
  const [personId, setPersonId] = useState<string | null>(null)
  const [give, setGive] = useState<{
    open: boolean
    recipient: UserSelectValue | null
  }>({ open: false, recipient: null })
  const [scopesDialog, setScopesDialog] = useState<{
    open: boolean
    target: ApiAccessScopesTarget | null
  }>({ open: false, target: null })
  const [removeTarget, setRemoveTarget] = useState<AgentApiAccessEntry | null>(
    null,
  )
  const [removeGroupTarget, setRemoveGroupTarget] =
    useState<AgentApiAccessGroupEntry | null>(null)

  const accessQuery = useAgentApiAccess(agentId)
  const entries = useMemo(
    () => sortApiAccessEntries(accessQuery.data?.data ?? NO_ENTRIES),
    [accessQuery.data],
  )
  const groups = accessQuery.data?.groups ?? NO_GROUPS
  const externalOn =
    accessQuery.data?.external_access_enabled ?? externalAccessEnabled
  const identityOn = accessQuery.data?.identity_enabled ?? identityEnabled

  const pausedKeyCount = externalOn
    ? 0
    : entries.reduce(
        (n, e) => n + summarizeApiAccessPerson(e).liveKeys.length,
        0,
      )

  // The person dialog reads its person from this query; if a refetch drops
  // them (their last key deleted by its holder), the dialog closes.
  const person = personId
    ? entries.find((e) => e.user.id === personId)
    : undefined
  if (personId && accessQuery.data && !person) setPersonId(null)

  const invalidateAgent = () => {
    queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    queryClient.invalidateQueries({ queryKey: ["agents"] })
    queryClient.invalidateQueries({ queryKey: agentApiAccessQueryKey(agentId) })
  }

  const externalMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateAgent({
        id: agentId,
        requestBody: { agent_api_external_access_enabled: enabled },
      }),
    onSuccess: (_res, enabled) =>
      showSuccessToast(
        enabled ? "External access turned on" : "External access paused",
      ),
    onError: handleError.bind(showErrorToast),
    onSettled: invalidateAgent,
  })

  const identityMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateAgent({
        id: agentId,
        requestBody: { agent_api_identity_enabled: enabled },
      }),
    onSuccess: (_res, enabled) =>
      showSuccessToast(
        enabled ? "Per-user scopes turned on" : "Per-user scopes turned off",
      ),
    onError: handleError.bind(showErrorToast),
    onSettled: invalidateAgent,
  })

  const removeScopesMutation = useMutation({
    mutationFn: ({ grantId }: { grantId: string }) =>
      AgentApiService.deleteAgentApiGrant({ agentId, grantId }),
    onSuccess: () => showSuccessToast("Scopes removed"),
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({
        queryKey: agentApiAccessQueryKey(agentId),
      })
    },
  })
  const removingGrantId = removeScopesMutation.isPending
    ? removeScopesMutation.variables?.grantId
    : undefined

  const removeGroupMutation = useMutation({
    mutationFn: ({ grantId }: { grantId: string }) =>
      AgentApiService.deleteAgentApiGroupGrant({ agentId, grantId }),
    onSuccess: () => showSuccessToast("Group scopes removed"),
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({
        queryKey: agentApiAccessQueryKey(agentId),
      })
      // The same grant shows on the Bundle Permissions card of a bundle
      // consuming this producer.
      queryClient.invalidateQueries({ queryKey: ["bundlePermissionsOverview"] })
      invalidateUserGroupShareLists(queryClient)
    },
  })
  const removingGroupGrantId = removeGroupMutation.isPending
    ? removeGroupMutation.variables?.grantId
    : undefined

  const openGive = (recipient: UserSelectValue | null) =>
    setGive({ open: true, recipient })

  const renderRow = (entry: AgentApiAccessEntry) => (
    <ApiAccessPersonRow
      entry={entry}
      canGiveKey={externalOn}
      scopesEnabled={identityOn}
      pendingLabel={
        entry.grant_id && removingGrantId === entry.grant_id
          ? "Removing scopes…"
          : null
      }
      onOpen={() => setPersonId(entry.user.id)}
      onGiveKey={() => openGive(asRecipient(entry))}
      onEditScopes={() =>
        setScopesDialog({ open: true, target: { kind: "person", entry } })
      }
      onRemoveScopes={() => setRemoveTarget(entry)}
    />
  )

  const renderGroupRow = (group: AgentApiAccessGroupEntry) => (
    <ApiAccessGroupRow
      group={group}
      scopesEnabled={identityOn}
      pendingLabel={
        removingGroupGrantId === group.grant_id ? "Removing scopes…" : null
      }
      onEditScopes={() =>
        setScopesDialog({ open: true, target: { kind: "group", group } })
      }
      onRemove={() => setRemoveGroupTarget(group)}
    />
  )

  const giveButton = (
    <Button size="sm" disabled={!externalOn} onClick={() => openGive(null)}>
      <Plus className="mr-2 h-4 w-4" />
      Give access
    </Button>
  )

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <KeyRound className="h-5 w-5 shrink-0" />
            API access
          </CardTitle>
          <div className="shrink-0">
            {externalOn ? (
              giveButton
            ) : (
              // A disabled button receives no pointer events, so the tooltip
              // hangs off a wrapper (as on EnvironmentCard).
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="inline-flex">{giveButton}</span>
                </TooltipTrigger>
                <TooltipContent side="top" className="text-xs">
                  Turn on External access to give keys
                </TooltipContent>
              </Tooltip>
            )}
          </div>
        </div>
        <CardDescription>
          People who can call this agent's API with a key, and what they or
          their groups may do.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <ApiAccessSwitchLine
          id={`api-access-external-${agentId}`}
          label="External access"
          hint={
            externalOn
              ? "Issued keys work. Turn off to pause every key at once."
              : "Every issued key is paused until you turn this back on."
          }
          checked={externalOn}
          disabled={externalMutation.isPending}
          onChange={(v) => externalMutation.mutate(v)}
        />
        <ApiAccessSwitchLine
          id={`api-access-identity-${agentId}`}
          label="Per-user scopes"
          hint={
            identityOn ? (
              <>
                Callers carry the scopes you grant; your API reads them via{" "}
                <code className="text-[11px]">caller.scopes</code>.
              </>
            ) : (
              "Callers are identified but carry no scopes."
            )
          }
          checked={identityOn}
          disabled={identityMutation.isPending}
          onChange={(v) => identityMutation.mutate(v)}
        />

        <Separator />

        {pausedKeyCount > 0 && (
          <Alert>
            <PauseCircle className="h-4 w-4" />
            <AlertDescription>
              {pausedKeyCount === 1
                ? "1 key is paused while external access is off."
                : `${pausedKeyCount} keys are paused while external access is off.`}
            </AlertDescription>
          </Alert>
        )}

        <section className="space-y-2">
          {/* Named only when a Groups section sits under it. */}
          {groups.length > 0 && <h3 className="text-sm font-medium">People</h3>}
          <PreviewList
            items={entries}
            getKey={(e) => e.user.id}
            renderItem={renderRow}
            isLoading={accessQuery.isLoading}
            isError={accessQuery.isError}
            error={accessQuery.error}
            onRetry={() => accessQuery.refetch()}
            errorFallback="Couldn't load who has API access"
            empty={
              <p className="text-sm text-muted-foreground">
                {groups.length > 0
                  ? "No people have API access yet."
                  : "Nobody has API access yet."}
              </p>
            }
            onShowAll={() => setSheetOpen(true)}
          />
        </section>

        {groups.length > 0 && (
          <section className="space-y-2">
            <h3 className="text-sm font-medium">Groups</h3>
            <PreviewList
              items={groups}
              getKey={(g) => g.grant_id}
              renderItem={renderGroupRow}
              // Rendered only from loaded data that holds a group.
              errorFallback="Couldn't load groups"
              empty={null}
              onShowAll={() => setSheetOpen(true)}
            />
          </section>
        )}

        {identityOn && (
          <Button
            variant="link"
            size="sm"
            className="px-0"
            onClick={() => setScopesDialog({ open: true, target: null })}
          >
            Grant scopes without a key
          </Button>
        )}
      </CardContent>

      <AllApiAccessSheet
        entries={entries}
        groups={groups}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        renderRow={renderRow}
        renderGroupRow={renderGroupRow}
      />

      {person && (
        <ApiAccessPersonDialog
          agentId={agentId}
          agentName={agentName}
          entry={person}
          open
          onOpenChange={(open) => {
            if (!open) setPersonId(null)
          }}
          canGiveKey={externalOn}
          onGiveKey={() => {
            setPersonId(null)
            openGive(asRecipient(person))
          }}
        />
      )}

      <AgentApiKeyDialog
        open={give.open}
        onOpenChange={(open) => setGive((g) => ({ ...g, open }))}
        producerAgentId={agentId}
        initialRecipient={give.recipient}
      />

      <ApiAccessScopesDialog
        agentId={agentId}
        open={scopesDialog.open}
        onOpenChange={(open) => setScopesDialog((d) => ({ ...d, open }))}
        target={scopesDialog.target}
        excludedUserIds={entries
          .filter((e) => !!e.grant_id)
          .map((e) => e.user.id)}
        excludedGroupIds={groups.map((g) => g.group_id)}
      />

      <AlertDialog
        open={!!removeTarget}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              {`Remove ${
                removeTarget ? apiAccessPersonLabel(removeTarget) : "their"
              }'s scopes on ${agentName}?`}
            </AlertDialogTitle>
            <AlertDialogDescription>
              Their keys keep working but carry no scopes. Revoke keys from
              their access details.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (removeTarget?.grant_id) {
                  removeScopesMutation.mutate({
                    grantId: removeTarget.grant_id,
                  })
                }
                setRemoveTarget(null)
              }}
            >
              Remove scopes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <RemoveGroupScopesDialog
        group={removeGroupTarget}
        agentName={agentName}
        onOpenChange={(open) => {
          if (!open) setRemoveGroupTarget(null)
        }}
        onConfirm={(group) => {
          removeGroupMutation.mutate({ grantId: group.grant_id })
          setRemoveGroupTarget(null)
        }}
      />
    </Card>
  )
}
