import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Check, ChevronDown, ChevronRight, Copy, Upload } from "lucide-react"
import { useState } from "react"

import type { SkillEntryPublic, SkillPackageRevisionPublic } from "@/client"
import { SkillsService } from "@/client"
import { SkillCatalogErrorAlert } from "@/components/Catalog/SkillCatalogErrorAlert"
import { TooltipToggleItem } from "@/components/Common/TooltipToggleItem"
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
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import {
  SKILL_VISIBILITY_OPTIONS,
  skillRevisionLabel,
} from "@/utils/skillCatalog"

interface PublishSkillDialogProps {
  agentId: string
  skill: SkillEntryPublic
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Share this skill on the server catalog" — S9.
 *
 * A Create story with three visible fields, so it is one dialog rather than a
 * wizard; the `package_id` has a sensible server-side default and lives behind
 * the one "Advanced" disclosure §1 allows.
 *
 * Reachable from the Skills card row's `⋯` **only**. It must never open from
 * S2's SKILL.md viewer: that is a dialog, and a dialog does not open a dialog
 * (§2 "Disclosure depth", A2). The same rule shapes the end of the flow — the
 * success state replaces this dialog's body and *links* to the catalog page
 * instead of opening one.
 */
export function PublishSkillDialog({
  agentId,
  skill,
  open,
  onOpenChange,
}: PublishSkillDialogProps) {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()

  const [version, setVersion] = useState("")
  const [releaseNotes, setReleaseNotes] = useState("")
  // `null` means "the publisher has not touched this control". It cannot be
  // seeded with `useState(existing?.visibility)`: `existing` arrives from an
  // async query *after* the dialog mounts, so a seeded state would keep the
  // "private" default it was initialised with and silently unpublish a public
  // package on the next republish.
  const [visibilityDraft, setVisibilityDraft] = useState<string | null>(null)
  const [packageIdDraft, setPackageIdDraft] = useState("")
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [published, setPublished] = useState<SkillPackageRevisionPublic | null>(
    null,
  )
  const [copied, setCopied] = useState(false)

  // Is this a republish? The catalog list is the only place that answers it —
  // there is no "package for this skill name" route — and it is already the
  // query the catalog page uses, so an open dialog costs at most one fetch.
  // Matched on `can_manage` as well as the name because names are unique *per
  // publisher*: somebody else's package of the same name is not this skill's.
  const { data: catalog, isLoading: isCatalogLoading } = useQuery({
    queryKey: ["skills-catalog"],
    queryFn: () => SkillsService.listSkillCatalog(),
    enabled: open,
  })
  const existing = catalog?.data.find(
    (entry) => entry.can_manage && entry.name === skill.name,
  )

  // What the toggle shows: the publisher's choice, else the package's current
  // visibility on a republish, else the default for a first publish.
  const visibility = visibilityDraft ?? existing?.visibility ?? "private"

  // After a first publish the response carries only the revision, whose
  // `package_id` is the package's UUID. The handle the publisher pastes into a
  // README comes from the package itself, so the success panel reads it back.
  const { data: publishedPackage } = useQuery({
    // A placeholder id rather than `undefined`: the key stays stable and
    // inert either way, but `undefined` makes every un-published dialog share
    // one cache entry.
    queryKey: ["skills-catalog", "package", published?.package_id ?? "none"],
    queryFn: () =>
      // `enabled` guarantees this is set; `?? ""` keeps the call typed without
      // asserting the narrowing away.
      SkillsService.getSkillPackage({
        packageId: published?.package_id ?? "",
      }),
    enabled: !!published,
  })

  const publishMutation = useMutation({
    mutationFn: () =>
      SkillsService.publishAgentSkill({
        agentId,
        name: skill.name,
        requestBody: {
          version: version.trim() || null,
          release_notes: releaseNotes.trim() || null,
          // The draft, never the derived display value, and deliberately not
          // gated on `existing`: that arrives from an async query, so between
          // mount and resolution the gate would be false and this would post
          // "private" — silently unpublishing a public package on the ordinary
          // "open the dialog, press Publish" path, since every field here is
          // optional. `null` is the right thing to send for "untouched" in
          // both cases: the backend's `if visibility is not None` leaves a
          // republished package alone, and a first publish is created private
          // by default anyway. `existing` stays a *display* concern.
          visibility: visibilityDraft,
          // Only ever sent on a first publish: the id is immutable, and
          // re-sending it is refused with `package_id_immutable`.
          package_id: existing ? null : packageIdDraft.trim() || null,
        },
      }),
    onSuccess: (revision) => {
      setPublished(revision)
      queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
      // The index now knows this skill has a package behind it.
      queryClient.invalidateQueries({ queryKey: ["agent", agentId, "skills"] })
    },
  })

  const isPending = publishMutation.isPending
  // Until the republish lookup settles, the dialog cannot tell a first publish
  // from a republish: it would offer the Advanced package-id field that a
  // republish must not have, and omit the "Republishing … as revision N" line.
  // Deliberately `isLoading`, not "no data": a *failed* lookup must not lock
  // the button forever — it falls through to the first-publish shape, and a
  // wrong package id comes back as a coded `package_id_immutable` the alert
  // already explains.
  const isResolvingPackage = isCatalogLoading
  const handle = publishedPackage?.package_id ?? null

  const copyPackageId = async () => {
    if (!handle) return
    try {
      await navigator.clipboard.writeText(handle)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Failed to copy the package id")
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md">
        {published ? (
          <>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2 min-w-0">
                <Upload className="h-5 w-5 shrink-0" />
                <span className="truncate">
                  Published {skill.name} {skillRevisionLabel(published)}
                </span>
              </DialogTitle>
              <DialogDescription>
                It is in the skills catalog now. Anyone who can see it can add
                it to one of their agents.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-3">
              <Button asChild variant="link" className="h-auto px-0">
                <Link
                  to="/catalog/skills/$packageId"
                  params={{ packageId: published.package_id }}
                >
                  Open in the skills catalog
                </Link>
              </Button>
              {handle && (
                <div className="space-y-1">
                  <span className="text-xs text-muted-foreground">
                    Package id
                  </span>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 rounded border bg-background px-2 py-1.5 font-mono text-xs break-all">
                      {handle}
                    </code>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="icon"
                          className="h-8 w-8 shrink-0"
                          aria-label="Copy package id"
                          onClick={copyPackageId}
                        >
                          {copied ? (
                            <Check className="h-3 w-3" />
                          ) : (
                            <Copy className="h-3 w-3" />
                          )}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Copy package id</TooltipContent>
                    </Tooltip>
                  </div>
                </div>
              )}
            </div>

            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                Close
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2 min-w-0">
                <Upload className="h-5 w-5 shrink-0" />
                <span className="truncate">Publish {skill.name}</span>
              </DialogTitle>
              <DialogDescription>
                The skill folder is snapshotted as a new revision. Published
                revisions are immutable.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4">
              {existing && (
                <p className="text-xs text-muted-foreground">
                  Republishing{" "}
                  <span className="font-mono">{existing.package_id}</span> as
                  revision {(existing.latest_revision_number ?? 0) + 1}.
                </p>
              )}

              <div className="space-y-1.5">
                <Label htmlFor="publish-skill-version">Version</Label>
                <Input
                  id="publish-skill-version"
                  value={version}
                  disabled={isPending}
                  placeholder="1.0.0"
                  onChange={(e) => setVersion(e.target.value)}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="publish-skill-notes">
                  Release notes{" "}
                  <span className="text-muted-foreground">(optional)</span>
                </Label>
                <Textarea
                  id="publish-skill-notes"
                  rows={3}
                  value={releaseNotes}
                  disabled={isPending}
                  placeholder="What changed since the last revision."
                  onChange={(e) => setReleaseNotes(e.target.value)}
                />
              </div>

              <div className="space-y-1.5">
                <Label>Visibility</Label>
                <ToggleGroup
                  type="single"
                  variant="outline"
                  size="sm"
                  value={visibility}
                  disabled={isPending}
                  onValueChange={(next) => next && setVisibilityDraft(next)}
                  aria-label="Who can install this package"
                >
                  {SKILL_VISIBILITY_OPTIONS.map((option) => (
                    <TooltipToggleItem
                      key={option.value}
                      value={option.value}
                      label={option.hint}
                    >
                      {option.label}
                    </TooltipToggleItem>
                  ))}
                </ToggleGroup>
                {/* The toggle already shows the package's current visibility
                    on a republish, so this says the one thing the control
                    cannot: the change is not scoped to this revision. Shown
                    only once the publisher has actually moved it. */}
                {existing && visibilityDraft !== null && (
                  <p className="text-xs text-muted-foreground">
                    This changes the whole package, not just this revision.
                  </p>
                )}
              </div>

              {/* The one Advanced disclosure, holding the one field with a
                  server-side default. Hidden entirely on a republish: the id is
                  immutable, and an editable field that can only be refused is
                  worse than no field. */}
              {!existing && (
                <div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="px-0 text-muted-foreground"
                    onClick={() => setAdvancedOpen((prev) => !prev)}
                    aria-expanded={advancedOpen}
                  >
                    {advancedOpen ? (
                      <ChevronDown className="h-4 w-4" />
                    ) : (
                      <ChevronRight className="h-4 w-4" />
                    )}
                    Advanced
                  </Button>
                  {advancedOpen && (
                    <div className="space-y-1.5 pt-2">
                      <Label htmlFor="publish-skill-package-id">
                        Package id
                      </Label>
                      <Input
                        id="publish-skill-package-id"
                        value={packageIdDraft}
                        disabled={isPending}
                        placeholder="com.example.my-skill"
                        onChange={(e) => setPackageIdDraft(e.target.value)}
                      />
                      <p className="text-xs text-muted-foreground">
                        Leave it empty and the server derives one. It can never
                        be changed afterwards — every install references it.
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* The secret scan runs again server-side, so a stale index can
                  still produce `skill_contains_secrets` here — with the
                  offending paths, which is what this alert renders. */}
              {publishMutation.isError && (
                <SkillCatalogErrorAlert
                  error={publishMutation.error}
                  fallback="Couldn't publish the skill"
                />
              )}
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={isPending}
              >
                Cancel
              </Button>
              <LoadingButton
                type="button"
                loading={isPending}
                disabled={isResolvingPackage}
                onClick={() => publishMutation.mutate()}
              >
                Publish
              </LoadingButton>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
