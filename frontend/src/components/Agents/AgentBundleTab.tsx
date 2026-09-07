/**
 * AgentBundleTab — publisher-facing bundle management.
 *
 * Two cards side-by-side:
 *   - LEFT  Bundle settings — catalog visibility, allowlist, listed flag,
 *           and default install update mode. Empty before first publish.
 *   - RIGHT Revisions — Bundle ID display (post-publish, locked) at the
 *           top, "Publish revision" button in the header corner, and a
 *           house list of revisions (the first five, "Show all" for the rest).
 *
 * Bundle ID is set once, in the publish dialog, on the first publish —
 * we no longer offer a separate edit modal because it's locked the
 * moment a bundle exists. The publish dialog also takes a manual
 * version label (default "1.0", auto-bumped from the previous
 * revision afterwards).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertCircle,
  AlertTriangle,
  Check,
  Copy,
  History,
  Plus,
  Settings2,
} from "lucide-react"
import { useEffect, useState } from "react"

import { type AgentPublic, BundlesService, InstallsService } from "@/client"
import { AllRevisionsSheet } from "@/components/Agents/AllRevisionsSheet"
import { BundlePermissionsCard } from "@/components/Agents/BundlePermissionsCard"
import { BundleRevisionRow } from "@/components/Agents/BundleRevisionRow"
import { CredentialProvisioningSection } from "@/components/Agents/CredentialProvisioningSection"
import { PreviewList } from "@/components/Common/PreviewList"
import { providedByLabel } from "@/components/Credentials/providedByLabel"
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"

interface AgentBundleTabProps {
  agent: AgentPublic
}

const DEFAULT_FIRST_VERSION = "1.0"

// Increment the trailing numeric component of a dotted version string.
// "1.0" → "1.1", "2.5" → "2.6", "1" → "1.1". Falls back to "<v>.1" when
// no numeric tail is detectable.
function suggestNextVersion(prev: string | null | undefined): string {
  if (!prev) return DEFAULT_FIRST_VERSION
  const trimmed = prev.trim()
  if (!trimmed) return DEFAULT_FIRST_VERSION
  const parts = trimmed.split(".")
  if (parts.length < 2) return `${trimmed}.1`
  const tail = parts[parts.length - 1]
  const n = Number.parseInt(tail, 10)
  if (Number.isNaN(n) || String(n) !== tail) return `${trimmed}.1`
  parts[parts.length - 1] = String(n + 1)
  return parts.join(".")
}

export function AgentBundleTab({ agent }: AgentBundleTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [publishOpen, setPublishOpen] = useState(false)
  const [releaseNotes, setReleaseNotes] = useState("")
  const [bundleIdDraft, setBundleIdDraft] = useState(agent.bundle_id)
  const [versionDraft, setVersionDraft] = useState(DEFAULT_FIRST_VERSION)
  const [publishToPublicCatalog, setPublishToPublicCatalog] = useState(false)
  const [publishError, setPublishError] = useState<string | null>(null)
  // Publisher-facing notices from the last publish in this session.
  const [publishNotices, setPublishNotices] = useState<string[]>([])
  const [copiedBundleId, setCopiedBundleId] = useState(false)
  const [allRevisionsOpen, setAllRevisionsOpen] = useState(false)

  const isPublished = !!agent.bundle_uuid

  // Bundle metadata (only present once published).
  const { data: bundle } = useQuery({
    queryKey: ["bundles", agent.bundle_uuid],
    queryFn: () =>
      BundlesService.getBundle({ bundleUuid: agent.bundle_uuid as string }),
    enabled: !!agent.bundle_uuid,
  })

  // Revisions (post-publish).
  const {
    data: revisions,
    isLoading: revisionsLoading,
    isError: revisionsIsError,
    error: revisionsError,
    refetch: refetchRevisions,
  } = useQuery({
    queryKey: ["bundles", agent.bundle_uuid, "revisions"],
    queryFn: () =>
      BundlesService.listRevisions({
        bundleUuid: agent.bundle_uuid as string,
      }),
    enabled: !!agent.bundle_uuid,
  })

  // Credential-sharing drift — live vs the latest published snapshot. Drives
  // a "republish to apply" nudge in the Revisions card (mirrors the per-row
  // hint in CredentialProvisioningSection).
  const { data: credentialDrift } = useQuery({
    queryKey: ["bundle-credential-drift", agent.id],
    queryFn: () =>
      InstallsService.getBundleCredentialDrift({ agentId: agent.id }),
    enabled: agent.is_publisher_install && !!agent.bundle_uuid,
  })

  const driftedCredentials = (credentialDrift?.drift ?? []).filter(
    (d) => d.drifted,
  )
  const showDriftWarning =
    isPublished &&
    credentialDrift?.stale === true &&
    driftedCredentials.length > 0

  // The whole history, newest first: the card previews the house cap of it and
  // the Sheet shows the rest, so neither host slices the list itself.
  const allRevisions = revisions?.data ?? []
  const previousVersion = allRevisions[0]?.version ?? null

  // Reset publish-form fields when the dialog opens so each publish
  // starts from a fresh (and correctly suggested) baseline.
  useEffect(() => {
    if (publishOpen) {
      setBundleIdDraft(agent.bundle_id)
      setVersionDraft(
        isPublished
          ? suggestNextVersion(previousVersion)
          : DEFAULT_FIRST_VERSION,
      )
      setReleaseNotes("")
      setPublishToPublicCatalog(false)
      setPublishError(null)
    }
  }, [publishOpen, isPublished, agent.bundle_id, previousVersion])

  // ── Mutations ───────────────────────────────────────────────

  const publishMutation = useMutation({
    mutationFn: () =>
      InstallsService.publishAgent({
        agentId: agent.id,
        requestBody: {
          release_notes: releaseNotes || null,
          version: versionDraft.trim() || null,
          // Only forward bundle_id on the first publish — backend ignores
          // it after that and rejects mismatches with 409.
          bundle_id: !isPublished ? bundleIdDraft.trim() || null : null,
        },
      }),
    onSuccess: async (rev) => {
      const label = rev.version
        ? `version ${rev.version}`
        : `revision ${rev.revision_number}`
      const wasFirstPublish = !isPublished
      if (wasFirstPublish && publishToPublicCatalog) {
        try {
          await BundlesService.updateBundle({
            bundleUuid: rev.bundle_id,
            requestBody: { visibility: "public", is_listed: true },
          })
          showSuccessToast(`Published ${label} to public catalog`)
        } catch (e: any) {
          showErrorToast(
            e?.body?.detail ||
              "Published, but the bundle is still private — toggle visibility in Bundle settings.",
          )
        }
      } else {
        showSuccessToast(`Published ${label}`)
      }
      // Server-stated, and kept on the page rather than folded into the toast.
      // These say what this publish means for the people who will install it —
      // e.g. that an admin-provisioned AI credential cannot travel with the
      // bundle and installers will supply their own key. A toast that vanishes
      // in four seconds is not where you tell somebody a fact they need to put
      // in their release notes.
      setPublishNotices(rev.publish_notices)
      setPublishOpen(false)
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["bundles"] })
      queryClient.invalidateQueries({ queryKey: ["catalog"] })
    },
    onError: (e: any) => {
      setPublishError(e?.body?.detail || "Failed to publish")
    },
  })

  const updateBundleMutation = useMutation({
    mutationFn: (patch: {
      visibility?: string
      is_listed?: boolean
      default_install_mode?: string
    }) =>
      BundlesService.updateBundle({
        bundleUuid: agent.bundle_uuid as string,
        requestBody: patch,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["bundles", agent.bundle_uuid],
      })
      queryClient.invalidateQueries({ queryKey: ["bundles"] })
      queryClient.invalidateQueries({ queryKey: ["catalog"] })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to update bundle")
    },
  })

  const handleCopyBundleId = async () => {
    try {
      await navigator.clipboard.writeText(agent.bundle_id)
      setCopiedBundleId(true)
      setTimeout(() => setCopiedBundleId(false), 2000)
    } catch {
      showErrorToast("Failed to copy")
    }
  }

  // ── Render ──────────────────────────────────────────────────

  // Defensive guard: this tab is a publisher-only management surface.
  // Consumer / foreign installs — including a publisher who installs their
  // own bundle as a consumer (``is_publisher_install=false``) — must never
  // see publish / visibility / revision controls. The route already filters
  // the tab out for these installs; this prevents the controls from leaking
  // if the component is ever mounted directly.
  if (!!agent.bundle_uuid && !agent.is_publisher_install) return null

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* LEFT: Bundle settings — catalog settings only (visible after first publish). */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 min-w-0">
              <Settings2 className="h-5 w-5" />
              Bundle settings
            </CardTitle>
            <CardDescription>
              Catalog visibility and update behaviour for this bundle.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {!isPublished ? (
              <p className="text-sm text-muted-foreground py-2">
                Catalog settings appear after the first publish — publish the
                bundle from the Revisions card to enable visibility, allowlist,
                listing, and default update mode.
              </p>
            ) : (
              bundle && (
                <>
                  <div className="flex items-start justify-between gap-4 py-2">
                    <div className="min-w-0">
                      <Label className="text-sm font-medium">Visibility</Label>
                      <p className="text-xs text-muted-foreground">
                        Who can see this bundle in the catalog.
                      </p>
                    </div>
                    <Select
                      value={bundle.visibility}
                      onValueChange={(val) =>
                        updateBundleMutation.mutate({ visibility: val })
                      }
                    >
                      <SelectTrigger className="w-[260px] shrink-0">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="private">
                          Private — only you
                        </SelectItem>
                        <SelectItem value="users">Users — allowlist</SelectItem>
                        <SelectItem value="public">
                          Public — anyone on this instance
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {bundle.visibility === "users" && (
                    <p className="pl-1 text-xs text-muted-foreground">
                      Manage who can see this bundle in the{" "}
                      <span className="rounded bg-muted px-1.5 py-0.5 font-medium text-foreground">
                        Permissions management
                      </span>{" "}
                      card below.
                    </p>
                  )}

                  {bundle.visibility !== "private" && (
                    <div className="flex items-start justify-between gap-4 py-2">
                      <div className="min-w-0">
                        <Label className="text-sm font-medium">
                          Listed in catalog
                        </Label>
                        <p className="text-xs text-muted-foreground">
                          When off, the bundle is hidden from the catalog
                          regardless of visibility.
                        </p>
                      </div>
                      <div className="w-[260px] shrink-0 flex justify-end">
                        <Switch
                          checked={bundle.is_listed}
                          onCheckedChange={(checked) =>
                            updateBundleMutation.mutate({ is_listed: checked })
                          }
                        />
                      </div>
                    </div>
                  )}

                  <div className="flex items-start justify-between gap-4 py-2">
                    <div className="min-w-0">
                      <Label className="text-sm font-medium">
                        Default install update mode
                      </Label>
                      <p className="text-xs text-muted-foreground">
                        Whether new installs apply updates manually or
                        automatically.
                      </p>
                    </div>
                    <Select
                      value={bundle.default_install_mode}
                      onValueChange={(val) =>
                        updateBundleMutation.mutate({
                          default_install_mode: val,
                        })
                      }
                    >
                      <SelectTrigger className="w-[260px] shrink-0">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="manual">
                          Manual — user applies
                        </SelectItem>
                        <SelectItem value="automatic">
                          Automatic — apply on idle
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </>
              )
            )}
          </CardContent>
        </Card>

        {/* RIGHT: Revisions — primary action lives in the header corner. */}
        <Card>
          <CardHeader>
            <div className="flex items-start justify-between gap-3">
              <div className="space-y-1.5 min-w-0">
                <CardTitle className="flex items-center gap-2 min-w-0">
                  <History className="h-5 w-5" />
                  Revisions
                </CardTitle>
                <CardDescription>
                  {isPublished
                    ? "Append-only history. Each publish snapshots the workspace and notifies foreign installs."
                    : "Snapshot the current workspace (scripts, docs, knowledge, files, requirements) and make it available for install."}
                </CardDescription>
              </div>
              <Button size="sm" onClick={() => setPublishOpen(true)}>
                <Plus className="h-4 w-4 mr-1" />
                {isPublished ? "Publish revision" : "Publish"}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {/* What the publish that just happened means for installers.
                The readiness gate says a related thing to the *installer*, on
                their setup screen — a different person at a different moment,
                and one who cannot act on it. This is the publisher's copy. */}
            {publishNotices.length > 0 && (
              <div className="rounded-lg border border-amber-300 bg-amber-50 dark:border-amber-900/60 dark:bg-amber-950/30 px-3 py-2 space-y-1">
                <div className="flex items-center justify-between gap-2 text-amber-800 dark:text-amber-300">
                  <div className="flex items-center gap-2">
                    <AlertTriangle className="h-4 w-4 shrink-0" />
                    <span className="text-sm font-medium">
                      What people who install this will get
                    </span>
                  </div>
                  <button
                    type="button"
                    className="text-xs underline shrink-0"
                    onClick={() => setPublishNotices([])}
                  >
                    Dismiss
                  </button>
                </div>
                <ul className="space-y-0.5 pl-6 list-disc">
                  {publishNotices.map((notice) => (
                    <li
                      key={notice}
                      className="text-xs text-amber-700 dark:text-amber-300"
                    >
                      {notice}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Credential-sharing drift — a republish nudge when the live
                sharing model differs from the latest published snapshot. */}
            {showDriftWarning && (
              <div className="rounded-lg border border-amber-300 bg-amber-50 dark:border-amber-900/60 dark:bg-amber-950/30 px-3 py-2 space-y-1">
                <div className="flex items-center gap-2 text-amber-800 dark:text-amber-300">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  <span className="text-sm font-medium">
                    Republish to apply credential sharing changes
                  </span>
                </div>
                <ul className="space-y-0.5 pl-6 list-disc">
                  {driftedCredentials.map((d) => (
                    <li
                      key={d.name}
                      className="text-xs text-amber-700 dark:text-amber-300"
                    >
                      Credentials {d.name} sharing differs from the latest
                      published bundle (bundle:{" "}
                      {providedByLabel(d.snapshot_provided_by)})
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Bundle ID — locked, shown only after first publish. */}
            {isPublished && (
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
            )}

            {!isPublished ? (
              <p className="text-sm text-muted-foreground">
                Not published yet — publishing will create the first revision.
              </p>
            ) : (
              <PreviewList
                items={allRevisions}
                getKey={(rev) => rev.id}
                renderItem={(rev) => (
                  <BundleRevisionRow
                    agentId={agent.id}
                    bundleUuid={agent.bundle_uuid as string}
                    rev={rev}
                    isCurrent={bundle?.latest_revision_id === rev.id}
                    isInstalled={
                      agent.installed_revision_number === rev.revision_number
                    }
                  />
                )}
                isLoading={revisionsLoading}
                isError={revisionsIsError}
                error={revisionsError}
                onRetry={() => refetchRevisions()}
                errorFallback="Couldn't load revisions."
                empty={
                  <p className="text-sm text-muted-foreground">
                    No revisions yet.
                  </p>
                }
                onShowAll={() => setAllRevisionsOpen(true)}
              />
            )}
          </CardContent>
        </Card>
      </div>

      {/* Phase 5 — publisher-only credential provisioning controls, with the
          unified permissions card sitting right next to it in the same row. */}
      {agent.is_publisher_install && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <CredentialProvisioningSection agent={agent} bundle={bundle} />
          {isPublished && agent.bundle_uuid && (
            <BundlePermissionsCard
              agent={agent}
              bundleUuid={agent.bundle_uuid}
            />
          )}
        </div>
      )}

      {isPublished && agent.bundle_uuid && (
        <AllRevisionsSheet
          agentId={agent.id}
          bundleUuid={agent.bundle_uuid}
          revisions={allRevisions}
          currentRevisionId={bundle?.latest_revision_id}
          installedRevisionNumber={agent.installed_revision_number}
          open={allRevisionsOpen}
          onOpenChange={setAllRevisionsOpen}
        />
      )}

      {/* Publish dialog — bundle ID (first publish only) + version + release notes. */}
      <Dialog open={publishOpen} onOpenChange={setPublishOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Publish {isPublished ? "new revision" : "agent"}
            </DialogTitle>
            <DialogDescription>
              This will snapshot your current workspace including any debug data
              in <code className="font-mono">scripts/</code> or{" "}
              <code className="font-mono">docs/</code>. Make sure you've cleaned
              up before publishing.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            {!isPublished && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="publish-bundle-id">Bundle ID</Label>
                  <Input
                    id="publish-bundle-id"
                    value={bundleIdDraft}
                    onChange={(e) => {
                      setBundleIdDraft(e.target.value)
                      if (publishError) setPublishError(null)
                    }}
                    placeholder="io.example.bundle.abc12345"
                    className="font-mono text-sm"
                  />
                  <p className="text-xs text-muted-foreground">
                    Reverse-DNS identifier for this bundle. Locked once the
                    first revision is published.
                  </p>
                </div>

                <div className="flex items-start justify-between gap-4 rounded-lg border px-3 py-2">
                  <div className="min-w-0 space-y-0.5">
                    <Label
                      htmlFor="publish-public-catalog"
                      className="text-sm font-medium"
                    >
                      Publish in Public Catalog
                    </Label>
                    <p className="text-xs text-muted-foreground">
                      Make this bundle visible to all users on this instance and
                      list it in the catalog. You can change this later in
                      Bundle settings.
                    </p>
                  </div>
                  <Switch
                    id="publish-public-catalog"
                    checked={publishToPublicCatalog}
                    onCheckedChange={setPublishToPublicCatalog}
                  />
                </div>
              </>
            )}

            <div className="space-y-2">
              <Label htmlFor="publish-version">Version</Label>
              <Input
                id="publish-version"
                value={versionDraft}
                onChange={(e) => {
                  setVersionDraft(e.target.value)
                  if (publishError) setPublishError(null)
                }}
                placeholder={DEFAULT_FIRST_VERSION}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                {isPublished
                  ? `Auto-suggested as a minor bump from ${
                      previousVersion
                        ? `v${previousVersion}`
                        : "the previous revision"
                    } — change it for major releases.`
                  : `Default ${DEFAULT_FIRST_VERSION} for the first release — change it if you want a different starting version.`}
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="publish-notes">Release notes (optional)</Label>
              <Textarea
                id="publish-notes"
                value={releaseNotes}
                onChange={(e) => setReleaseNotes(e.target.value)}
                placeholder="What changed in this revision?"
                rows={4}
              />
            </div>

            {publishError && (
              <Alert variant="destructive" className="py-2">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription className="text-sm">
                  {publishError}
                </AlertDescription>
              </Alert>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPublishOpen(false)}
              disabled={publishMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              onClick={() => publishMutation.mutate()}
              disabled={
                publishMutation.isPending ||
                !versionDraft.trim() ||
                (!isPublished && !bundleIdDraft.trim())
              }
            >
              {publishMutation.isPending ? "Publishing..." : "Publish"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
