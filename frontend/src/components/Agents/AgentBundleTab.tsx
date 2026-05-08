/**
 * AgentBundleTab — publisher-facing bundle management.
 *
 * Two cards side-by-side:
 *   - LEFT  Bundle settings — catalog visibility, allowlist, listed flag,
 *           and default install update mode. Empty before first publish.
 *   - RIGHT Revisions — Bundle ID display (post-publish, locked) at the
 *           top, "Publish revision" button in the header corner, and a
 *           compact list of the latest 10 revisions.
 *
 * Bundle ID is set once, in the publish dialog, on the first publish —
 * we no longer offer a separate edit modal because it's locked the
 * moment a bundle exists. The publish dialog also takes a manual
 * version label (default "1.0", auto-bumped from the previous
 * revision afterwards).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertCircle, Check, Copy, Plus, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"

import {
  BundlesService,
  InstallsService,
  type AgentBundleRevisionPublic,
  type AgentPublic,
} from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { CredentialProvisioningSection } from "@/components/Agents/CredentialProvisioningSection"
import { UserAllowlistPicker } from "@/components/Common/UserAllowlistPicker"
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
import { Badge } from "@/components/ui/badge"
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
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

interface AgentBundleTabProps {
  agent: AgentPublic
}

const REVISIONS_LIMIT = 10
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
  const [publishError, setPublishError] = useState<string | null>(null)
  const [copiedBundleId, setCopiedBundleId] = useState(false)
  const [copiedHashId, setCopiedHashId] = useState<string | null>(null)
  const [deleteRevisionTarget, setDeleteRevisionTarget] = useState<{
    id: string
    label: string
  } | null>(null)

  const isPublished = !!agent.bundle_uuid

  // Bundle metadata (only present once published).
  const { data: bundle } = useQuery({
    queryKey: ["bundles", agent.bundle_uuid],
    queryFn: () =>
      BundlesService.getBundle({ bundleUuid: agent.bundle_uuid as string }),
    enabled: !!agent.bundle_uuid,
  })

  // Revisions (post-publish).
  const { data: revisions } = useQuery({
    queryKey: ["bundles", agent.bundle_uuid, "revisions"],
    queryFn: () =>
      BundlesService.listRevisions({
        bundleUuid: agent.bundle_uuid as string,
      }),
    enabled: !!agent.bundle_uuid,
  })

  // Grants (only fetched when visibility is "users").
  const { data: grants } = useQuery({
    queryKey: ["bundles", agent.bundle_uuid, "grants"],
    queryFn: () =>
      BundlesService.listGrants({
        bundleUuid: agent.bundle_uuid as string,
      }),
    enabled: !!agent.bundle_uuid && bundle?.visibility === "users",
  })

  const recentRevisions = (revisions?.data ?? []).slice(0, REVISIONS_LIMIT)
  const totalRevisions = revisions?.data?.length ?? 0
  const previousVersion = revisions?.data?.[0]?.version ?? null

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
    onSuccess: (rev) => {
      const label = rev.version ? `version ${rev.version}` : `revision ${rev.revision_number}`
      showSuccessToast(`Published ${label}`)
      setPublishOpen(false)
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["bundles"] })
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

  const addGrantMutation = useMutation({
    mutationFn: (email: string) =>
      BundlesService.addGrant({
        bundleUuid: agent.bundle_uuid as string,
        requestBody: { email },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["bundles", agent.bundle_uuid, "grants"],
      })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to add grant")
    },
  })

  const revokeGrantMutation = useMutation({
    mutationFn: (grantId: string) =>
      BundlesService.revokeGrant({
        bundleUuid: agent.bundle_uuid as string,
        grantId,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["bundles", agent.bundle_uuid, "grants"],
      })
    },
  })

  const deleteRevisionMutation = useMutation({
    mutationFn: (revisionId: string) =>
      BundlesService.deleteRevision({
        bundleUuid: agent.bundle_uuid as string,
        revisionId,
      }),
    onSuccess: () => {
      showSuccessToast("Revision deleted")
      setDeleteRevisionTarget(null)
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["bundles", agent.bundle_uuid] })
      queryClient.invalidateQueries({
        queryKey: ["bundles", agent.bundle_uuid, "revisions"],
      })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to delete revision")
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

  const handleCopyHash = async (hash: string, id: string) => {
    try {
      await navigator.clipboard.writeText(hash)
      setCopiedHashId(id)
      setTimeout(() => setCopiedHashId(null), 2000)
    } catch {
      showErrorToast("Failed to copy")
    }
  }

  const renderRevisionLabel = (rev: AgentBundleRevisionPublic) =>
    rev.version ? `v${rev.version}` : `rev ${rev.revision_number}`

  // ── Render ──────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* LEFT: Bundle settings — catalog settings only (visible after first publish). */}
        <Card>
          <CardHeader>
            <CardTitle>Bundle settings</CardTitle>
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
                        <SelectItem value="private">Private — only you</SelectItem>
                        <SelectItem value="users">Users — allowlist</SelectItem>
                        <SelectItem value="public">
                          Public — anyone on this instance
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {bundle.visibility === "users" && (
                    <div className="pl-1">
                      <UserAllowlistPicker
                        enabled={bundle.visibility === "users"}
                        selected={(grants?.data ?? []).map((g) => ({
                          id: g.id,
                          userId: g.user_id,
                          fallbackLabel: g.user_email ?? undefined,
                        }))}
                        onAdd={(u) => addGrantMutation.mutate(u.email)}
                        onRemove={(item) => revokeGrantMutation.mutate(item.id)}
                        isAdding={addGrantMutation.isPending}
                        isRemoving={revokeGrantMutation.isPending}
                        emptyHint="Nobody can see this bundle yet — add users above."
                      />
                    </div>
                  )}

                  {bundle.visibility !== "private" && (
                    <div className="flex items-start justify-between gap-4 py-2">
                      <div className="min-w-0">
                        <Label className="text-sm font-medium">
                          Listed in catalog
                        </Label>
                        <p className="text-xs text-muted-foreground">
                          When off, the bundle is hidden from the catalog regardless
                          of visibility.
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
                <CardTitle>Revisions</CardTitle>
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
            ) : recentRevisions.length === 0 ? (
              <p className="text-sm text-muted-foreground">No revisions yet.</p>
            ) : (
              <div className="space-y-1.5">
                {recentRevisions.map((rev) => {
                  const isCurrent = bundle?.latest_revision_id === rev.id
                  const isInstalled =
                    agent.installed_revision_number === rev.revision_number
                  const canDelete = (rev.install_count ?? 0) <= 1
                  return (
                    <div
                      key={rev.id}
                      className="flex items-center justify-between gap-3 px-3 py-2 border rounded-lg"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="font-medium text-sm shrink-0">
                          {renderRevisionLabel(rev)}
                        </span>
                        {rev.version && (
                          <span className="text-xs text-muted-foreground shrink-0">
                            (rev {rev.revision_number})
                          </span>
                        )}
                        {isCurrent && (
                          <Badge className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600">
                            current
                          </Badge>
                        )}
                        {isInstalled && !isCurrent && (
                          <Badge variant="secondary" className="text-xs shrink-0">
                            installed
                          </Badge>
                        )}
                        <span className="text-xs text-muted-foreground shrink-0">
                          {rev.install_count} install
                          {rev.install_count === 1 ? "" : "s"}
                        </span>
                        {rev.release_notes && (
                          <span className="text-xs text-muted-foreground truncate">
                            — {rev.release_notes}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        <span className="text-xs text-muted-foreground mr-1">
                          {new Date(rev.published_at).toLocaleDateString()}
                        </span>
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7"
                                onClick={() => handleCopyHash(rev.content_hash, rev.id)}
                                aria-label="Copy content hash"
                              >
                                {copiedHashId === rev.id ? (
                                  <Check className="h-3.5 w-3.5 text-green-500" />
                                ) : (
                                  <Copy className="h-3.5 w-3.5" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              <span className="font-mono">
                                {rev.content_hash.slice(0, 16)}…
                              </span>
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 text-destructive hover:text-destructive"
                                  disabled={!canDelete}
                                  onClick={() =>
                                    setDeleteRevisionTarget({
                                      id: rev.id,
                                      label: renderRevisionLabel(rev),
                                    })
                                  }
                                  aria-label="Delete revision"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </span>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              {canDelete
                                ? "Delete revision"
                                : `Cannot delete — ${rev.install_count} installs reference it`}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      </div>
                    </div>
                  )
                })}
                {totalRevisions > REVISIONS_LIMIT && (
                  <p className="text-xs text-muted-foreground pt-1">
                    Showing the latest {REVISIONS_LIMIT} of {totalRevisions}{" "}
                    revisions.
                  </p>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Phase 5 — publisher-only credential provisioning controls. */}
      {agent.is_publisher_install && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <CredentialProvisioningSection agent={agent} bundle={bundle} />
        </div>
      )}

      {/* Delete revision confirmation. */}
      <AlertDialog
        open={!!deleteRevisionTarget}
        onOpenChange={(open) => {
          if (!open) setDeleteRevisionTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Delete {deleteRevisionTarget?.label}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the revision row, its on-disk snapshot,
              and rewires the bundle's "current" pointer to the previous
              revision if needed. The publisher install stays — it just won't
              be on this revision anymore. The API rejects deletion if any
              other user's install still references this revision.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteRevisionMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (deleteRevisionTarget) {
                  deleteRevisionMutation.mutate(deleteRevisionTarget.id)
                }
              }}
              disabled={deleteRevisionMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteRevisionMutation.isPending
                ? "Deleting..."
                : "Delete revision"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Publish dialog — bundle ID (first publish only) + version + release notes. */}
      <Dialog open={publishOpen} onOpenChange={setPublishOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Publish {isPublished ? "new revision" : "agent"}
            </DialogTitle>
            <DialogDescription>
              This will snapshot your current workspace including any debug
              data in <code className="font-mono">scripts/</code> or{" "}
              <code className="font-mono">docs/</code>. Make sure you've
              cleaned up before publishing.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            {!isPublished && (
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
                      previousVersion ? `v${previousVersion}` : "the previous revision"
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
