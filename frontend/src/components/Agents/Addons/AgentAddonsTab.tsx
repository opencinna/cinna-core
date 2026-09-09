import { useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, CheckCircle2, XCircle, Zap } from "lucide-react"
import { useEffect, useState } from "react"

import type { PluginSyncResponse } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import { EventTypes, eventService } from "@/services/eventService"
import { invalidateAddons } from "@/utils/addons"
import { AddonsCard } from "./AddonsCard"

/** One plugin failure carried by a PLUGIN_SYNC_WARNING event. */
interface PluginSyncFailure {
  marketplace_name: string
  plugin_name: string
  source?: string
  error_message?: string | null
}

/** Aggregated live plugin-sync warning surfaced above the card. */
interface PluginSyncWarning {
  environmentId: string
  instanceName: string
  failures: PluginSyncFailure[]
}

interface AgentAddonsTabProps {
  agentId: string
}

/**
 * The Addons tab — one card, plus the two things that are about the *tab*
 * rather than about any row.
 *
 * A single-column `space-y-6` stack, not a card grid, exactly as the Plugins
 * tab it replaces was: the card takes the full tab width and A8 does not apply.
 *
 * Rendered for consumers of a foreign install too. That is not decoration: this
 * tab is now the only place a consumer can see what an installed agent can
 * actually do, since the Configuration tab's Skills card moved here. The server
 * answers `can_add=false` / `can_manage=false` / `can_share=false` there and
 * the card degrades on its own, so the gate is the tab list in
 * `routes/_layout/agent/$agentId.tsx`, never a `useRole()` check here.
 *
 * The live sync banner is rebuilt from the hand-rolled amber `div` the Plugins
 * tab carried (`border-amber-300 bg-amber-50 …`) onto `Alert` + the `--warning`
 * tokens: R14a has no established-idiom exemption now that the tokens exist,
 * and R11 forbids the hand-rolled equivalent of a shared primitive.
 */
export function AgentAddonsTab({ agentId }: AgentAddonsTabProps) {
  const queryClient = useQueryClient()

  // Live plugin-sync warnings, keyed by environment id (latest wins per env).
  const [syncWarnings, setSyncWarnings] = useState<
    Record<string, PluginSyncWarning>
  >({})

  const [syncProgress, setSyncProgress] = useState<{
    isOpen: boolean
    title: string
    syncResult: PluginSyncResponse | null
  }>({ isOpen: false, title: "", syncResult: null })

  // A plugin that failed to install on env start/rebuild. The projection is
  // now wrong about what the engine actually loaded, so it is invalidated
  // alongside the banner. `AGENT_UPDATED` is already handled at the route,
  // which prefix-invalidates `["agent", agentId]` and therefore this list too.
  useEffect(() => {
    if (!agentId) return
    const subId = eventService.subscribe(
      EventTypes.PLUGIN_SYNC_WARNING,
      (event) => {
        if (event.model_id && event.model_id !== agentId) return
        const meta = event.meta || {}
        const environmentId = String(meta.environment_id || "")
        const failures = Array.isArray(meta.failures)
          ? (meta.failures as PluginSyncFailure[])
          : []
        if (!environmentId || failures.length === 0) return
        setSyncWarnings((prev) => ({
          ...prev,
          [environmentId]: {
            environmentId,
            instanceName: String(meta.instance_name || environmentId),
            failures,
          },
        }))
        invalidateAddons(queryClient, agentId)
      },
    )
    return () => eventService.unsubscribe(subId)
  }, [agentId, queryClient])

  /**
   * The one thing the rows cannot own: the dialog that lists a partial sync
   * failure. A row keeps its own pending state (a shared `useMutation`
   * describes only its latest call) and hands the failure report up here.
   */
  const reportSyncResult = (title: string, result: PluginSyncResponse) => {
    setSyncProgress({ isOpen: true, title, syncResult: result })
  }

  const activeWarnings = Object.values(syncWarnings)

  return (
    <div className="space-y-6">
      {activeWarnings.map((warning) => (
        <Alert
          key={warning.environmentId}
          className="border-warning text-warning"
        >
          <AlertTriangle />
          <AlertTitle>
            Some plugins failed to install in {warning.instanceName}
          </AlertTitle>
          <AlertDescription>
            <p className="text-sm">The environment started without them.</p>
            <ul className="list-inside list-disc space-y-0.5 text-sm">
              {warning.failures.map((failure, index) => (
                <li
                  key={`${failure.marketplace_name}/${failure.plugin_name}-${index}`}
                >
                  <span className="font-medium">
                    {failure.marketplace_name}/{failure.plugin_name}
                  </span>
                  {failure.error_message ? `: ${failure.error_message}` : ""}
                </li>
              ))}
            </ul>
            <Button
              variant="outline"
              size="sm"
              className="mt-1 h-7 text-xs"
              onClick={() =>
                setSyncWarnings((prev) => {
                  const next = { ...prev }
                  delete next[warning.environmentId]
                  return next
                })
              }
            >
              Dismiss
            </Button>
          </AlertDescription>
        </Alert>
      ))}

      <AddonsCard agentId={agentId} onSyncResult={reportSyncResult} />

      {/* Chained after the mutation settles, never nested in it: a dialog that
          reports what a *finished* write did is the "chained is not nested"
          case §2 allows. */}
      <Dialog
        open={syncProgress.isOpen}
        onOpenChange={(open) => {
          if (!open) {
            setSyncProgress({ isOpen: false, title: "", syncResult: null })
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-warning" />
              {syncProgress.title} — sync issues
            </DialogTitle>
            <DialogDescription>
              {syncProgress.syncResult?.message}
            </DialogDescription>
          </DialogHeader>
          {syncProgress.syncResult && (
            <div className="mt-2 space-y-3">
              {(syncProgress.syncResult.environments_synced?.length ?? 0) >
                0 && (
                <div className="space-y-2">
                  <p className="text-sm font-medium">Environment status</p>
                  <div className="max-h-60 space-y-1 overflow-y-auto">
                    {syncProgress.syncResult.environments_synced?.map((env) => (
                      <div
                        key={env.environment_id}
                        className="flex items-center justify-between gap-2 rounded-md bg-muted p-2 text-sm"
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          {env.status === "success" ||
                          env.status === "activated_and_synced" ? (
                            <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />
                          ) : env.status === "unsupported" ? (
                            // The link write worked and the container is too
                            // old to take it: a rebuild, not a retry. Warning
                            // tone, because the backend deliberately keeps this
                            // out of `failed_syncs`.
                            <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />
                          ) : (
                            <XCircle className="h-4 w-4 shrink-0 text-destructive" />
                          )}
                          <span className="truncate">{env.instance_name}</span>
                          {env.was_suspended && (
                            <Badge variant="outline" className="h-5 shrink-0">
                              <Zap className="mr-1 h-3 w-3" />
                              Activated
                            </Badge>
                          )}
                        </div>
                        {env.error_message && (
                          <span
                            className={`max-w-[200px] truncate text-xs ${
                              env.status === "unsupported"
                                ? "text-warning"
                                : "text-destructive"
                            }`}
                          >
                            {env.error_message}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {/* Per-plugin install failures (env reached, but a plugin could
                  not be fetched or seeded — excluded from the active set). */}
              {(syncProgress.syncResult.plugin_results?.length ?? 0) > 0 && (
                <div className="space-y-2">
                  <p className="text-sm font-medium">Plugin failures</p>
                  <div className="max-h-60 space-y-1 overflow-y-auto">
                    {syncProgress.syncResult.plugin_results?.map(
                      (plugin, index) => (
                        <div
                          key={`${plugin.marketplace_name}/${plugin.plugin_name}-${index}`}
                          className="flex items-center justify-between gap-2 rounded-md bg-muted p-2 text-sm"
                        >
                          <div className="flex min-w-0 items-center gap-2">
                            <XCircle className="h-4 w-4 shrink-0 text-destructive" />
                            <span className="truncate">
                              {plugin.marketplace_name}/{plugin.plugin_name}
                            </span>
                          </div>
                          {plugin.error_message && (
                            <span className="max-w-[200px] truncate text-xs text-destructive">
                              {plugin.error_message}
                            </span>
                          )}
                        </div>
                      ),
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
          <DialogFooter>
            <Button
              onClick={() =>
                setSyncProgress({
                  isOpen: false,
                  title: "",
                  syncResult: null,
                })
              }
            >
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
