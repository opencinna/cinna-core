/**
 * BundleInstallationCard — the durable home for a consumer install's bundle
 * provenance and its update preferences.
 *
 * Visibility: foreign installs only (``bundle_uuid`` set and
 * ``is_publisher_install`` false). Publisher installs manage the bundle from
 * the Bundle tab instead, and non-bundle agents have nothing to show.
 *
 * Deliberately NOT gated by ``AgentConfigTab``'s ``readOnly`` flag. That flag
 * exists because bundle *content* (prompts, description) is publisher-authored
 * and must not be edited by the consumer. The update mode is the opposite kind
 * of setting: it is the consumer's own preference about their own install, so
 * it stays editable even on a read-only configuration tab.
 *
 * Unlike ``UpdateAvailableBanner`` — which only queries ``check-updates`` once
 * ``pending_update`` is already true — this card queries without waiting for
 * that flag, so it can render an explicit "Up to date" state rather than
 * silently showing nothing. The query is still gated on the visibility rule
 * above; see the comment at the ``useQuery`` call for why.
 */
import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Copy,
  Download,
  Package,
} from "lucide-react"

import type { AgentPublic } from "@/client"
import { InstallsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { revisionLabel } from "@/utils/bundleRevision"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

interface BundleInstallationCardProps {
  agent: AgentPublic
}

const UPDATE_MODE_MANUAL = "manual"
const UPDATE_MODE_AUTOMATIC = "automatic"

export function BundleInstallationCard({ agent }: BundleInstallationCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copiedBundleId, setCopiedBundleId] = useState(false)

  const isForeignInstall = !!agent.bundle_uuid && !agent.is_publisher_install

  // Shares its query key with UpdateAvailableBanner, so the two surfaces
  // resolve to one request. Unconditional in the sense that matters (see the
  // module docstring): it does NOT wait for ``pending_update``, so the card can
  // render an explicit "Up to date". It is still gated on the D9 visibility
  // predicate — ``check-updates`` is a POST that commits a reconciliation, and
  // AgentConfigTab is the default tab of every agent page, so firing it for
  // agents that render no card at all would be a write on every page load.
  const {
    data: updateInfo,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["agent", agent.id, "check-updates"],
    queryFn: () => InstallsService.checkUpdates({ agentId: agent.id }),
    enabled: isForeignInstall,
    staleTime: 30_000,
  })

  // ``invalidateQueries`` matches by key prefix, so this also refreshes
  // ``["agent", id, "check-updates"]``.
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
  }

  // ``check_for_updates`` reconciles ``pending_update`` server-side. When the
  // response disagrees with the cached agent row — a revision was published
  // while this page sat open — refresh the agent row so UpdateAvailableBanner,
  // which reads ``agent.pending_update``, agrees with this card. ``exact``
  // keeps the invalidation off this query's own key, which shares the prefix.
  const pendingUpdate = updateInfo?.pending_update
  useEffect(() => {
    if (pendingUpdate === undefined) return
    if (pendingUpdate === !!agent.pending_update) return
    queryClient.invalidateQueries({
      queryKey: ["agent", agent.id],
      exact: true,
    })
  }, [pendingUpdate, agent.pending_update, agent.id, queryClient])

  const applyMutation = useMutation({
    mutationFn: () => InstallsService.applyUpdate({ agentId: agent.id }),
    onSuccess: () => {
      showSuccessToast("Update applied")
      invalidate()
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to apply update")
    },
  })

  // Mirrors AgentBundleTab's copy handler, including the 2s confirmation.
  const handleCopyBundleId = async () => {
    try {
      await navigator.clipboard.writeText(agent.bundle_id)
      setCopiedBundleId(true)
      setTimeout(() => setCopiedBundleId(false), 2000)
    } catch {
      showErrorToast("Failed to copy")
    }
  }

  const updateModeMutation = useMutation({
    mutationFn: (mode: string) =>
      InstallsService.setUpdateMode({
        agentId: agent.id,
        requestBody: { update_mode: mode },
      }),
    onSuccess: (_data, mode) => {
      showSuccessToast(
        mode === UPDATE_MODE_AUTOMATIC
          ? "Updates will be applied automatically"
          : "Updates will need to be applied manually",
      )
      invalidate()
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to change update mode")
    },
  })

  // D9 — foreign installs only. Hooks above stay unconditional.
  if (!isForeignInstall) return null

  // Read the installed revision from a single snapshot: the check-updates
  // response once it has resolved, the agent row until then. Falling back
  // field-by-field could pair one source's version label with the other's
  // revision number.
  const installedLabel =
    (updateInfo
      ? revisionLabel(
          updateInfo.installed_version,
          updateInfo.installed_revision_number,
        )
      : revisionLabel(
          agent.installed_revision_version,
          agent.installed_revision_number,
        )) ?? "unknown"
  const latestLabel = revisionLabel(
    updateInfo?.latest_version,
    updateInfo?.latest_revision_number,
  )

  // ``check_for_updates`` reconciles ``pending_update`` server-side against the
  // bundle's latest revision, so the response flag is the authoritative
  // "a newer revision exists" signal.
  const updateAvailable = !!updateInfo?.pending_update
  // Prefer the freshly-reconciled mode from check-updates; the agent row is the
  // fallback while the query is still in flight.
  const updateMode =
    updateInfo?.update_mode ?? agent.update_mode ?? UPDATE_MODE_MANUAL

  // A bare Card: AgentConfigTab lays every card out in one shared responsive
  // grid, so this component must be a direct grid item. Wrapping it in its own
  // grid would pin it to its own row and leave the cell beside it empty.
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Package className="h-5 w-5" />
          Bundle installation
        </CardTitle>
        <CardDescription>
          This agent was installed from a bundle. Its prompts and description
          are authored by the bundle publisher; you choose how their updates
          reach this install.
        </CardDescription>
      </CardHeader>
      {/* Label left, value/control right on every row — the same shape as the
          Usability card in AgentInterfaceTab. Labels are ``shrink-0`` so the
          long reverse-DNS bundle ID truncates instead of squeezing its own
          label. */}
      <CardContent className="space-y-4">
        {/* Bundle ID — same locked, copyable treatment as the Revisions card
            on the publisher's Bundle tab, so the identifier reads identically
            on both sides of the install. */}
        <div className="flex items-center gap-2 px-3 py-2 border rounded-lg bg-muted/30">
          <Label className="text-xs font-medium text-muted-foreground shrink-0">
            Bundle ID
          </Label>
          <code className="text-xs font-mono truncate flex-1">
            {agent.bundle_id}
          </code>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            onClick={handleCopyBundleId}
            title="Copy bundle ID"
            aria-label="Copy bundle ID"
          >
            {copiedBundleId ? (
              <Check className="h-3.5 w-3.5 text-green-500" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </Button>
        </div>

        <div className="flex items-center justify-between gap-4">
          <p className="shrink-0 text-sm font-medium">Installed version</p>
          <p className="text-sm text-muted-foreground">{installedLabel}</p>
        </div>

        {/* Latest available — one row, like the rows around it. Informational
            on purpose: UpdateAvailableBanner directly above the tabs is the
            page-level attention grabber, so a second amber alarm panel here
            would just double the noise. */}
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="text-sm font-medium">Latest available</p>
            {updateAvailable && updateInfo?.latest_published_at && (
              <p className="text-xs text-muted-foreground">
                Published{" "}
                <RelativeTime
                  timestamp={updateInfo.latest_published_at}
                  showTooltip
                />
              </p>
            )}
            {updateAvailable && updateInfo?.latest_release_notes && (
              <p className="line-clamp-2 text-xs text-muted-foreground">
                {updateInfo.latest_release_notes}
              </p>
            )}
          </div>

          {isLoading ? (
            <div className="h-5 w-24 shrink-0 animate-pulse rounded bg-muted" />
          ) : isError ? (
            <span className="flex shrink-0 items-center gap-1.5 text-sm text-muted-foreground">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              Check failed
            </span>
          ) : updateAvailable ? (
            <div className="flex shrink-0 items-center gap-2">
              {latestLabel && (
                <span className="text-sm text-muted-foreground">
                  {latestLabel}
                </span>
              )}
              {/* D10 — stays visible in automatic mode too. Automatic only
                  applies while the agent is idle, so an impatient consumer
                  still needs a way to pull the revision right now. */}
              <Button
                size="sm"
                className="gap-1.5"
                onClick={() => applyMutation.mutate()}
                disabled={applyMutation.isPending}
              >
                <Download className="h-3.5 w-3.5" />
                {applyMutation.isPending ? "Updating..." : "Update now"}
              </Button>
            </div>
          ) : (
            <span className="flex shrink-0 items-center gap-1.5 text-sm text-muted-foreground">
              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
              {latestLabel ? `${latestLabel} — up to date` : "Up to date"}
            </span>
          )}
        </div>

        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="text-sm font-medium">Update mode</p>
            <p className="text-xs text-muted-foreground">
              {updateMode === UPDATE_MODE_AUTOMATIC
                ? "Applied while the agent is idle, usually within ~10 minutes of a new release"
                : "New releases are announced here and applied only when you press Update now"}
            </p>
          </div>
          <Select
            value={updateMode}
            onValueChange={(value) => updateModeMutation.mutate(value)}
            disabled={updateModeMutation.isPending}
          >
            <SelectTrigger className="w-[130px] shrink-0">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={UPDATE_MODE_MANUAL}>Manual</SelectItem>
              <SelectItem value={UPDATE_MODE_AUTOMATIC}>Automatic</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </CardContent>
    </Card>
  )
}
