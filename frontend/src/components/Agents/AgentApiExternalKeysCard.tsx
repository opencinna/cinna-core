import { useMutation, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import { KeyRound, Lock, Plus, Trash2 } from "lucide-react"
import { useState } from "react"

import type { AgentApiKeyPublic } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
import { ListRow, ListRowGroup, RowFlag } from "@/components/Common/ListRow"
import { AgentApiKeyDialog } from "@/components/Credentials/AgentApiKeyDialog"
import { AGENT_API_KEY_LABEL_PLURAL } from "@/components/Credentials/agentApiKeyCopy"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { agentApiKeysQueryKey, useAgentApiKeys } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"

interface AgentApiExternalKeysCardProps {
  agentId: string
  /** Current value of the producer's external-access opt-in flag. */
  externalAccessEnabled: boolean
}

/**
 * External keys — the producer owner's audit view of everything outside the
 * platform that can reach this API, and the switch that decides whether
 * anything can at all. (The user-visible product name lives in
 * ``agentApiKeyCopy.ts``.)
 *
 * Sits beneath the Connections list on the "Agent REST API" card: connections
 * are the agents calling in, keys are the laptops, servers, and cron jobs. Both
 * go through the same proxy, the same ``policy.yaml``, and the same live scope
 * grant — the producer writes one authorization path, not two.
 *
 * The opt-in is deliberate: making an API reachable by a copy-pasteable bearer
 * key should be an explicit act, and the flag doubles as a kill switch (with it
 * off, every existing key stops working without being deleted).
 */
export function AgentApiExternalKeysCard({
  agentId,
  externalAccessEnabled,
}: AgentApiExternalKeysCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [createOpen, setCreateOpen] = useState(false)

  // Listed regardless of the switch. Turning external access off BLOCKS keys
  // rather than deleting them, so hiding the list with the switch would hide
  // exactly the keys an owner most wants to see — and revoke — after pulling
  // the kill switch.
  const { data: keysData, isLoading } = useAgentApiKeys(agentId)
  const keys: AgentApiKeyPublic[] = keysData?.data ?? []

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateAgent({
        id: agentId,
        requestBody: { agent_api_external_access_enabled: enabled },
      }),
    onSuccess: (_res, enabled) => {
      showSuccessToast(
        enabled ? "External access enabled" : "External access disabled",
      )
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      // is_usable folds the kill switch in, so every listed key's state changes
      // with the flag.
      queryClient.invalidateQueries({ queryKey: agentApiKeysQueryKey(agentId) })
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to update"),
  })

  const revokeMutation = useMutation({
    mutationFn: (keyId: string) =>
      AgentApiService.deleteAgentApiKey({ agentId, keyId }),
    onSuccess: () => {
      showSuccessToast("Key revoked")
      queryClient.invalidateQueries({ queryKey: agentApiKeysQueryKey(agentId) })
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to revoke key"),
  })

  const header = (
    <div className="flex items-start justify-between gap-2">
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-medium">
            {AGENT_API_KEY_LABEL_PLURAL}
          </span>
          {keys.length > 0 && (
            <Badge variant="secondary" className="text-xs">
              {keys.length}
            </Badge>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          {externalAccessEnabled
            ? "Keys let code outside the platform call this API. Each one acts as a specific user and carries that user's scopes. Turning this off blocks every key at once."
            : "Off by default. Turn on to issue keys that let a script, a server, or a cron job outside the platform call this API as a specific user."}
        </p>
      </div>
      <Switch
        checked={externalAccessEnabled}
        onCheckedChange={(v) => toggleMutation.mutate(v)}
        disabled={toggleMutation.isPending}
        className="mt-0.5"
      />
    </div>
  )

  // Nothing to show and nothing issuable — collapse to the switch alone.
  if (!externalAccessEnabled && keys.length === 0) {
    return <div className="rounded-lg border p-3 space-y-2">{header}</div>
  }

  return (
    <div className="rounded-lg border p-3 space-y-3">
      {header}

      {!externalAccessEnabled && keys.length > 0 && (
        <p className="text-xs text-amber-600 dark:text-amber-500">
          {keys.length === 1 ? "This key is" : `These ${keys.length} keys are`}{" "}
          blocked while external access is off. Turn it back on to make{" "}
          {keys.length === 1 ? "it" : "them"} work again, or revoke{" "}
          {keys.length === 1 ? "it" : "them"} below.
        </p>
      )}

      {isLoading ? (
        <p className="text-xs text-muted-foreground">Loading keys…</p>
      ) : keys.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No keys issued yet. Issue one and hand the value to its holder.
        </p>
      ) : (
        <ListRowGroup>
          {keys.map((key) => (
            <ExternalKeyRow
              key={key.id}
              apiKey={key}
              blocked={!externalAccessEnabled}
              disabled={revokeMutation.isPending}
              onRevoke={() => revokeMutation.mutate(key.id)}
            />
          ))}
        </ListRowGroup>
      )}

      {externalAccessEnabled && (
        <>
          <Button
            variant="outline"
            size="sm"
            className="h-8"
            onClick={() => setCreateOpen(true)}
          >
            <Plus className="h-4 w-4 mr-1.5" />
            Issue key
          </Button>

          <AgentApiKeyDialog
            open={createOpen}
            onOpenChange={setCreateOpen}
            producerAgentId={agentId}
          />
        </>
      )}
    </div>
  )
}

interface ExternalKeyRowProps {
  apiKey: AgentApiKeyPublic
  /**
   * The producer's kill switch is off. ``is_usable`` folds that in, so without
   * this the row would label every key "Inactive" and imply something is wrong
   * with the key itself rather than with the agent-wide switch.
   */
  blocked?: boolean
  disabled?: boolean
  onRevoke: () => void
}

/** One issued key: who it acts as, its prefix, its state, and Revoke. */
function ExternalKeyRow({
  apiKey,
  blocked,
  disabled,
  onRevoke,
}: ExternalKeyRowProps) {
  const subjectLabel =
    apiKey.subject?.full_name || apiKey.subject?.email || "Unknown user"
  const expired =
    !!apiKey.expires_at && new Date(apiKey.expires_at).getTime() <= Date.now()

  const usable = !expired && apiKey.is_usable && !blocked

  return (
    <ListRow
      muted={!usable}
      status={{
        tone: usable ? "on" : expired ? "warning" : "off",
        label: expired
          ? "Expired"
          : `${usable ? "Active" : "Inactive"} — ${
              apiKey.expires_at
                ? `expires ${formatDistanceToNow(new Date(apiKey.expires_at), { addSuffix: true })}`
                : "no expiry"
            }`,
      }}
      title={subjectLabel}
      // Three facts on one line rather than three stacked `<span>`s: the label
      // is the only one that identifies the key, so it leads.
      // The title is the *person*, so the prefix is the only thing telling two
      // of their keys apart — it stays. Expiry moved to the dot's tooltip.
      meta={[
        apiKey.label,
        `${apiKey.token_prefix}…`,
        apiKey.last_used_at
          ? `used ${formatDistanceToNow(new Date(apiKey.last_used_at), { addSuffix: true })}`
          : "never used",
      ]
        .filter(Boolean)
        .join(" · ")}
      flags={
        apiKey.read_only ? (
          <RowFlag
            icon={Lock}
            label="Read-only — this key cannot change anything"
          />
        ) : undefined
      }
    >
      {/* One action, and it is destructive, so it is hover-revealed rather
          than sitting in a `⋯` menu of one item (§2 Row actions). */}
      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
            disabled={disabled}
            aria-label={`Revoke the key issued to ${subjectLabel}`}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Revoke this key?</AlertDialogTitle>
            <AlertDialogDescription>
              Anything using <code>{apiKey.token_prefix}…</code> loses access
              immediately, and the key's credential is deleted. {subjectLabel}'s
              scopes on this agent are left untouched — they still apply to
              their own agents and any other key issued to them. This cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={onRevoke}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Revoke
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ListRow>
  )
}
