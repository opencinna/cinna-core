/**
 * AgentBundleTab — publisher-facing bundle management.
 *
 * Sections:
 *   1. Bundle ID display + edit (edit only allowed pre-publish, i.e. when
 *      no ``bundle_uuid`` is linked yet).
 *   2. Publish dialog with optional release notes.
 *   3. Catalog settings — visibility / listing / default-install-mode.
 *   4. Grants table for ``visibility="users"`` — add by email + revoke.
 *   5. Revisions list — revision_number, content_hash short, published_at,
 *      install count.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Copy, Trash2 } from "lucide-react"
import { useState } from "react"

import {
  BundlesService,
  InstallsService,
  type AgentPublic,
} from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
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

export function AgentBundleTab({ agent }: AgentBundleTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [publishOpen, setPublishOpen] = useState(false)
  const [releaseNotes, setReleaseNotes] = useState("")
  const [editingBundleId, setEditingBundleId] = useState(false)
  const [bundleIdDraft, setBundleIdDraft] = useState(agent.bundle_id)
  const [grantEmail, setGrantEmail] = useState("")

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

  // ── Mutations ───────────────────────────────────────────────

  const publishMutation = useMutation({
    mutationFn: () =>
      InstallsService.publishAgent({
        agentId: agent.id,
        requestBody: { release_notes: releaseNotes || null },
      }),
    onSuccess: (rev) => {
      showSuccessToast(`Published revision ${rev.revision_number}`)
      setReleaseNotes("")
      setPublishOpen(false)
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["bundles"] })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to publish")
    },
  })

  const editBundleIdMutation = useMutation({
    mutationFn: (newId: string) =>
      InstallsService.editBundleId({
        agentId: agent.id,
        requestBody: { bundle_id: newId },
      }),
    onSuccess: () => {
      showSuccessToast("Bundle ID updated")
      setEditingBundleId(false)
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to update bundle ID")
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
      setGrantEmail("")
      showSuccessToast("Grant added")
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

  // ── Render ──────────────────────────────────────────────────

  return (
    <div className="space-y-4">
      {/* Bundle ID */}
      <Card>
        <CardHeader>
          <CardTitle>Bundle identity</CardTitle>
          <CardDescription>
            The reverse-DNS identifier other users see in the catalog. Editing
            is locked once the agent has been published — otherwise any
            installed app-data on dependent installs would silently orphan.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {editingBundleId ? (
            <div className="space-y-2">
              <Input
                value={bundleIdDraft}
                onChange={(e) => setBundleIdDraft(e.target.value)}
                placeholder="io.example.bundle.abc12345"
                className="font-mono text-sm"
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={() => editBundleIdMutation.mutate(bundleIdDraft)}
                  disabled={
                    editBundleIdMutation.isPending ||
                    bundleIdDraft === agent.bundle_id
                  }
                >
                  {editBundleIdMutation.isPending ? "Saving..." : "Save"}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setEditingBundleId(false)
                    setBundleIdDraft(agent.bundle_id)
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <code className="font-mono text-xs bg-muted px-1.5 py-0.5 rounded break-all flex-1">
                {agent.bundle_id}
              </code>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => {
                  navigator.clipboard.writeText(agent.bundle_id)
                  showSuccessToast("Copied")
                }}
                aria-label="Copy bundle id"
              >
                <Copy className="h-4 w-4" />
              </Button>
              {!isPublished && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditingBundleId(true)}
                >
                  Edit
                </Button>
              )}
            </div>
          )}
          {isPublished && (
            <p className="text-xs text-muted-foreground">
              This bundle has already been published — bundle ID is locked.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Publish */}
      <Card>
        <CardHeader>
          <CardTitle>
            {isPublished ? "Publish a new revision" : "Publish this agent"}
          </CardTitle>
          <CardDescription>
            Snapshots the workspace folders (scripts, docs, knowledge, files,
            requirements) and makes them available for install.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {isPublished && agent.installed_revision_number && (
            <p className="text-sm">
              <span className="text-muted-foreground">Installed revision:</span>{" "}
              <code className="font-mono text-xs">
                v{agent.installed_revision_number}
              </code>
            </p>
          )}
          <Button onClick={() => setPublishOpen(true)}>
            {isPublished ? "Publish new revision" : "Publish"}
          </Button>
        </CardContent>
      </Card>

      {/* Catalog settings — only after first publish. */}
      {isPublished && bundle && (
        <Card>
          <CardHeader>
            <CardTitle>Catalog settings</CardTitle>
            <CardDescription>
              Control who sees this bundle in the catalog and how their installs
              receive updates.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label>Visibility</Label>
              <Select
                value={bundle.visibility}
                onValueChange={(val) =>
                  updateBundleMutation.mutate({ visibility: val })
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="private">Private — only you</SelectItem>
                  <SelectItem value="users">
                    Users — explicit allowlist below
                  </SelectItem>
                  <SelectItem value="public">
                    Public — anyone on this instance
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="flex items-center justify-between gap-3">
              <div>
                <Label>Listed in catalog</Label>
                <p className="text-xs text-muted-foreground">
                  When off, the bundle is not shown in the catalog regardless
                  of visibility.
                </p>
              </div>
              <Switch
                checked={bundle.is_listed}
                onCheckedChange={(checked) =>
                  updateBundleMutation.mutate({ is_listed: checked })
                }
              />
            </div>

            <div className="space-y-1.5">
              <Label>Default install update mode</Label>
              <Select
                value={bundle.default_install_mode}
                onValueChange={(val) =>
                  updateBundleMutation.mutate({ default_install_mode: val })
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual">
                    Manual — user applies updates
                  </SelectItem>
                  <SelectItem value="automatic">
                    Automatic — apply on idle env
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Grants — only when visibility=users. */}
      {isPublished && bundle?.visibility === "users" && (
        <Card>
          <CardHeader>
            <CardTitle>Access grants</CardTitle>
            <CardDescription>
              Users who can see and install this bundle from the catalog.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex gap-2">
              <Input
                type="email"
                placeholder="user@example.com"
                value={grantEmail}
                onChange={(e) => setGrantEmail(e.target.value)}
              />
              <Button
                onClick={() => addGrantMutation.mutate(grantEmail.trim())}
                disabled={!grantEmail.trim() || addGrantMutation.isPending}
              >
                Add
              </Button>
            </div>
            <ul className="space-y-1">
              {(grants?.data ?? []).length === 0 ? (
                <li className="text-sm text-muted-foreground">
                  No grants yet — nobody can see this bundle.
                </li>
              ) : (
                grants?.data.map((grant) => (
                  <li
                    key={grant.id}
                    className="flex items-center justify-between text-sm py-1"
                  >
                    <span>{grant.user_email ?? grant.user_id}</span>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => revokeGrantMutation.mutate(grant.id)}
                      aria-label="Revoke grant"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </li>
                ))
              )}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* Revisions list. */}
      {isPublished && (
        <Card>
          <CardHeader>
            <CardTitle>Revisions</CardTitle>
            <CardDescription>
              Append-only history. Foreign installs auto-update (or are flagged
              pending) when a new revision is published.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {revisions?.data && revisions.data.length > 0 ? (
              <ul className="space-y-2">
                {revisions.data.map((rev) => (
                  <li
                    key={rev.id}
                    className="flex items-start justify-between gap-3 border-b last:border-b-0 pb-2 last:pb-0"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">v{rev.revision_number}</span>
                        {bundle?.latest_revision_id === rev.id && (
                          <Badge variant="secondary" className="text-xs">
                            current
                          </Badge>
                        )}
                        <Badge variant="outline" className="text-xs font-normal">
                          {rev.install_count} install
                          {rev.install_count === 1 ? "" : "s"}
                        </Badge>
                      </div>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        Published {new Date(rev.published_at).toLocaleString()}
                      </p>
                      {rev.release_notes && (
                        <p className="text-sm mt-1">{rev.release_notes}</p>
                      )}
                      <code className="block font-mono text-[11px] text-muted-foreground mt-1 truncate">
                        {rev.content_hash.slice(0, 16)}…
                      </code>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No revisions yet.</p>
            )}
          </CardContent>
        </Card>
      )}

      {/* Publish dialog. */}
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
          <div className="space-y-2">
            <Label>Release notes (optional)</Label>
            <Textarea
              value={releaseNotes}
              onChange={(e) => setReleaseNotes(e.target.value)}
              placeholder="What changed in this revision?"
              rows={4}
            />
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
              disabled={publishMutation.isPending}
            >
              {publishMutation.isPending ? "Publishing..." : "Publish"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
