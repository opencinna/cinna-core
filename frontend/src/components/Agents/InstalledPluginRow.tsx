import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { formatDistanceToNow } from "date-fns"
import {
  ArrowUpCircle,
  ExternalLink,
  GraduationCap,
  Package,
  Power,
  PowerOff,
  Store,
  Trash2,
} from "lucide-react"
import { useState } from "react"

import type {
  AgentPluginLinkWithUpdateInfo,
  PluginSyncResponse,
} from "@/client"
import { LlmPluginsService } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { TooltipToggleItem } from "@/components/Common/TooltipToggleItem"
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
import { Badge } from "@/components/ui/badge"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { ToggleGroup } from "@/components/ui/toggle-group"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

/**
 * The row's two mode segments. Short labels because the whole group must stay
 * ~130px so the plugin name keeps its width at 1024 (§2 "Toggles on rows"); the
 * full sentence rides on the `aria-label` and the tooltip.
 */
const MODE_SEGMENTS = [
  {
    value: "conversation",
    short: "Chat",
    label: "Enabled in conversation mode",
  },
  { value: "building", short: "Build", label: "Enabled in building mode" },
]

/** A sync result the tab renders in its own dialog when something failed. */
export type SyncReporter = (title: string, result: PluginSyncResponse) => void

interface InstalledPluginRowProps {
  agentId: string
  plugin: AgentPluginLinkWithUpdateInfo
  /**
   * Hands an env-level or per-plugin sync failure back to the tab, which owns
   * the dialog that lists them. The row keeps its own pending state; only the
   * *reporting* of a partial failure is a tab-level concern.
   */
  onSyncResult: SyncReporter
}

/**
 * One installed plugin, as a house list row.
 *
 * Replaces the `<Table>` row that carried three `Switch`es, four `Badge`s and
 * an unconfirmed Uninstall button (A10). The shape is
 * `Admin/AccessPolicy/CompanyAiCredentialRow`'s: a status dot for on/off, one
 * badge, passive facts as `RowFlag`s, and a **segmented multi-toggle** as the
 * row's single inline control — which is one control for the budget because
 * Chat and Build are one field of one entity (§2 "Toggles on rows").
 *
 * The row owns its mutations rather than sharing the tab's. A shared
 * `useMutation` describes only its latest call, so `variables?.id === plugin.id`
 * unfreezes an in-flight row the moment a second row is clicked; per-row
 * observers cannot get that wrong.
 */
export function InstalledPluginRow({
  agentId,
  plugin,
  onSyncResult,
}: InstalledPluginRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmOpen, setConfirmOpen] = useState(false)

  const name = plugin.plugin_name || "Unknown plugin"
  const isBundle = plugin.source === "bundle"
  const isCatalog = plugin.source === "catalog"

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["agent-plugins", agentId] })

  /**
   * Everything an install-set change touches, not just this tab's list.
   *
   * A catalog skill also appears in the agent's skill index (`source:
   * "catalog"`) and in the catalog's own `installed_in_agent_ids` /
   * `install_count` — the same counters the install dialog invalidates, in the
   * other direction. Without this, uninstalling leaves the skills catalog's
   * "Installed" filter and its Installs fact wrong until a page reload. The
   * skills index is cache-only, so its refetch may return the same list until
   * the Skills card's Refresh re-reads the container.
   */
  const invalidateInstallSet = () => {
    invalidate()
    queryClient.invalidateQueries({ queryKey: ["agent", agentId, "skills"] })
    if (isCatalog) {
      queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
    }
  }

  /** Any partial failure goes to the tab's dialog; a clean run just toasts. */
  const settleSync = (
    result: PluginSyncResponse,
    title: string,
    ok: string,
  ) => {
    if (
      (result.failed_syncs && result.failed_syncs > 0) ||
      result.partial_failures
    ) {
      onSyncResult(title, result)
    } else {
      showSuccessToast(ok)
    }
  }

  const updateMutation = useMutation({
    mutationFn: (body: {
      conversation_mode?: boolean | null
      building_mode?: boolean | null
      disabled?: boolean | null
    }) =>
      LlmPluginsService.updateAgentPlugin({
        agentId,
        linkId: plugin.id,
        requestBody: body,
      }),
    onSuccess: (result, body) => {
      const title =
        body.disabled != null
          ? body.disabled
            ? "Plugin disabled"
            : "Plugin enabled"
          : "Plugin updated"
      settleSync(result, title, title)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to update the plugin")),
    onSettled: invalidate,
  })

  const upgradeMutation = useMutation({
    mutationFn: () =>
      LlmPluginsService.upgradeAgentPlugin({ agentId, linkId: plugin.id }),
    onSuccess: (result) =>
      settleSync(
        result,
        "Plugin upgraded",
        "Plugin upgraded to the latest version",
      ),
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to upgrade the plugin")),
    onSettled: invalidate,
  })

  const uninstallMutation = useMutation({
    mutationFn: () =>
      LlmPluginsService.uninstallAgentPlugin({ agentId, linkId: plugin.id }),
    onSuccess: () => {
      showSuccessToast(`${name} uninstalled`)
      setConfirmOpen(false)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to uninstall the plugin")),
    onSettled: invalidateInstallSet,
  })

  const isPending =
    updateMutation.isPending ||
    upgradeMutation.isPending ||
    uninstallMutation.isPending

  // The group is fully controlled off the server's two booleans, so Radix hands
  // back this exact array plus or minus exactly one entry — the caller wants
  // the one mode that moved, not the whole set.
  const modes = [
    ...(plugin.conversation_mode ? ["conversation"] : []),
    ...(plugin.building_mode ? ["building"] : []),
  ]

  const installedAt = (() => {
    try {
      return `Installed ${formatDistanceToNow(new Date(plugin.created_at), {
        addSuffix: true,
      })}`
    } catch {
      return null
    }
  })()

  const sourceFlag = isBundle ? (
    <RowFlag
      icon={Package}
      label="Delivered by the bundle — managed by its publisher"
    />
  ) : isCatalog ? (
    <RowFlag icon={GraduationCap} label="Installed from the skills catalog" />
  ) : (
    <RowFlag
      icon={Store}
      label={
        plugin.marketplace_name
          ? `From the marketplace ${plugin.marketplace_name}`
          : "From a marketplace"
      }
    />
  )

  return (
    <ListRow
      muted={plugin.disabled}
      status={{
        tone: plugin.disabled ? "off" : "on",
        label: plugin.disabled ? "Disabled" : "Enabled",
      }}
      title={name}
      // At most one badge. The version is the word people scan a plugin list
      // by; the source, the category and the description are flags or the
      // detail tooltip, which cost the name no width.
      badges={
        plugin.installed_version ? (
          <Badge variant="secondary" className="h-5">
            v{plugin.installed_version}
          </Badge>
        ) : undefined
      }
      flags={
        <>
          {sourceFlag}
          {plugin.has_update && (
            <RowFlag
              icon={ArrowUpCircle}
              tone="warning"
              label={`Update available — v${plugin.latest_version ?? "?"}`}
            />
          )}
          <RowInfo
            facts={[
              plugin.plugin_description,
              plugin.plugin_category,
              installedAt,
            ]}
          />
        </>
      }
    >
      <ToggleGroup
        type="multiple"
        variant="outline"
        size="sm"
        // Only this row: the write is per-link, so freezing the rest of the
        // list for one round trip would be a cost with no matching risk.
        disabled={isPending || plugin.disabled}
        value={modes}
        onValueChange={(next) => {
          // A plugin enabled in no mode is a plugin that is on and does
          // nothing: the row would still read "Enabled" while the engine never
          // loads it, and the install dialog refuses the same state at the
          // other end. The backend has no such guard, so the floor is here —
          // the last segment cannot be turned off, and its tooltip says which
          // control does turn the plugin off.
          if (next.length === 0) return
          const turnedOn = next.find((mode) => !modes.includes(mode))
          if (turnedOn) {
            updateMutation.mutate(
              turnedOn === "conversation"
                ? { conversation_mode: true }
                : { building_mode: true },
            )
            return
          }
          const turnedOff = modes.find((mode) => !next.includes(mode))
          if (turnedOff) {
            updateMutation.mutate(
              turnedOff === "conversation"
                ? { conversation_mode: false }
                : { building_mode: false },
            )
          }
        }}
        className="shrink-0"
        aria-label={`Modes ${name} is enabled in`}
      >
        {MODE_SEGMENTS.map((mode) => {
          const isOnlyMode = modes.length === 1 && modes[0] === mode.value
          return (
            <TooltipToggleItem
              key={mode.value}
              value={mode.value}
              label={mode.label}
              tooltip={
                isOnlyMode
                  ? `${mode.label} — the only mode it runs in. Use Disable in the menu to turn the plugin off.`
                  : mode.label
              }
              className="px-2"
            >
              {mode.short}
            </TooltipToggleItem>
          )
        })}
      </ToggleGroup>

      <RowActionsMenu label={`the plugin ${name}`} disabled={isPending}>
        {plugin.has_update && !isBundle && (
          <DropdownMenuItem
            onSelect={(e) => {
              e.preventDefault()
              upgradeMutation.mutate()
            }}
          >
            <ArrowUpCircle />
            Update to v{plugin.latest_version ?? "latest"}
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          onSelect={(e) => {
            e.preventDefault()
            updateMutation.mutate({ disabled: !plugin.disabled })
          }}
        >
          {plugin.disabled ? <Power /> : <PowerOff />}
          {plugin.disabled ? "Enable" : "Disable"}
        </DropdownMenuItem>
        {isCatalog && plugin.skill_package_id && (
          <DropdownMenuItem asChild>
            <Link
              to="/catalog/skills/$packageId"
              params={{ packageId: plugin.skill_package_id }}
            >
              <ExternalLink />
              Open in the skills catalog
            </Link>
          </DropdownMenuItem>
        )}
        {/* A bundle plugin has no uninstall of its own: it arrives and leaves
            with the bundle's apply-update, which is what the `Package` flag
            says. Enable/disable stays consumer-local. */}
        {!isBundle && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onSelect={(e) => {
                e.preventDefault()
                setConfirmOpen(true)
              }}
            >
              <Trash2 />
              Uninstall
            </DropdownMenuItem>
          </>
        )}
      </RowActionsMenu>

      {/* Owned by the row, not nested in the menu item: a `DropdownMenuItem`
          unmounts on select and would take the confirm's pending state with
          it. This replaces an Uninstall button that fired the mutation on the
          first click (R6). */}
      <AlertDialog
        open={confirmOpen}
        onOpenChange={(next) => {
          if (!uninstallMutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Uninstall {name}?</AlertDialogTitle>
            <AlertDialogDescription>
              The plugin is removed from this agent and from every environment
              it runs in. Nothing else on the agent changes, and you can install
              it again from the available plugins below.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={uninstallMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                uninstallMutation.mutate()
              }}
              disabled={uninstallMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {uninstallMutation.isPending ? "Uninstalling…" : "Uninstall"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ListRow>
  )
}
