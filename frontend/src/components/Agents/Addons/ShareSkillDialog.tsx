import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronDown, ChevronRight, Upload } from "lucide-react"
import { useState } from "react"

import type { SkillEntryPublic, SkillPackageRevisionPublic } from "@/client"
import { SkillsService } from "@/client"
import { SkillCatalogErrorAlert } from "@/components/Catalog/SkillCatalogErrorAlert"
import { TooltipToggleItem } from "@/components/Common/TooltipToggleItem"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Alert, AlertDescription } from "@/components/ui/alert"
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
import { invalidateAddons } from "@/utils/addons"
import { SKILL_VISIBILITY_OPTIONS } from "@/utils/skillCatalog"
import { ShareSkillSuccessPanel } from "./ShareSkillSuccessPanel"

interface ShareSkillDialogProps {
  agentId: string
  skill: SkillEntryPublic
  /**
   * The package this agent already published this skill as, from the addons
   * projection. It changes the verbs — sharing a skill for the first time and
   * appending a revision to an immutable package are different acts — and it
   * is what the existing-grants list keys on.
   */
  publishedPackageId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Make this skill available to everyone, to a few named colleagues, or to no
 * one yet" — S4.
 *
 * The former `PublishSkillDialog`, moved here and extended with the third
 * visibility. Still a Create story with one section, so it is one dialog rather
 * than a wizard; `package_id` keeps the one "Advanced" disclosure §1 allows.
 *
 * Reachable from the addon row's `⋯` **only**. It must never open from the
 * detail dialog: that is a dialog, and a dialog does not open a dialog (§2
 * "Disclosure depth", A2). The same rule shapes both ends of the flow — the
 * people picker is a `Popover` anchored to its own input rather than
 * `BundlePermissionsAddUserModal`, and the success state replaces this body and
 * *links* to the catalog page instead of opening one.
 */
export function ShareSkillDialog({
  agentId,
  skill,
  publishedPackageId,
  open,
  onOpenChange,
}: ShareSkillDialogProps) {
  const queryClient = useQueryClient()

  const [version, setVersion] = useState("")
  const [releaseNotes, setReleaseNotes] = useState("")
  // `null` means "the publisher has not touched this control". It cannot be
  // seeded with `useState(existing?.visibility)`: `existing` arrives from an
  // async query *after* the dialog mounts, so a seeded state would keep the
  // "private" default it was initialised with and silently unshare a public
  // package on the next republish.
  const [visibilityDraft, setVisibilityDraft] = useState<string | null>(null)
  const [packageIdDraft, setPackageIdDraft] = useState("")
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [people, setPeople] = useState<
    Array<{ id: string; email: string; label: string }>
  >([])
  const [published, setPublished] = useState<SkillPackageRevisionPublic | null>(
    null,
  )

  const isRepublish = publishedPackageId != null

  // The package's current visibility and its next revision number. The catalog
  // list is the only place that answers it — there is no "package for this
  // skill name" route — and it is already the query the catalog page uses, so
  // an open dialog costs at most one fetch. Matched on `can_manage` as well as
  // the name because names are unique *per publisher*: somebody else's package
  // of the same name is not this skill's.
  const { data: catalog, isLoading: isCatalogLoading } = useQuery({
    queryKey: ["skills-catalog"],
    queryFn: () => SkillsService.listSkillCatalog(),
  })
  // The authoritative id first, the name scan only as a fallback for a package
  // this projection has not caught up with. Matching on the name alone made
  // the two halves of "is this a republish" disagree: a same-named package
  // published from *another* of the user's agents would put the body into
  // republish mode under a "Share …" title, and a package missing from the
  // list (delisted, or a failed catalog read) would offer the immutable
  // package-id field on a genuine republish.
  const existing =
    catalog?.data.find((entry) => entry.id === publishedPackageId) ??
    catalog?.data.find((entry) => entry.can_manage && entry.name === skill.name)

  // What the toggle shows: the publisher's choice, else the package's current
  // visibility on a republish, else the default for a first share.
  const visibility = visibilityDraft ?? existing?.visibility ?? "private"
  const isUsersVisibility = visibility === "users"

  // Who can already see it. Read-only here on purpose: `grant_emails` is
  // additive server-side, and revoking belongs on the package's own page where
  // the consequence ("agents that already installed it keep working") is
  // spelled out beside the list.
  const { data: grants } = useQuery({
    queryKey: [
      "skills-catalog",
      "package",
      publishedPackageId ?? "none",
      "grants",
    ],
    queryFn: () =>
      SkillsService.listSkillPackageGrants({
        packageId: publishedPackageId ?? "",
      }),
    enabled: isRepublish && isUsersVisibility,
  })
  const existingGrants = grants?.data ?? []

  // After a first publish the response carries only the revision, whose
  // `package_id` is the package's UUID. The handle the publisher pastes into a
  // README comes from the package itself, so the success panel reads it back.
  const { data: publishedPackage } = useQuery({
    queryKey: ["skills-catalog", "package", published?.package_id ?? "none"],
    queryFn: () =>
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
          // "private" — silently unsharing a public package on the ordinary
          // "open the dialog, press Share" path, since every field here is
          // optional.
          visibility: visibilityDraft,
          // Additive server-side, and only meaningful for `users`: sending the
          // list while the publisher is on Public would create grants nobody
          // asked for and nobody can see (they are inert under `public`).
          grant_emails: isUsersVisibility
            ? people.map((person) => person.email)
            : [],
          // Only ever sent on a first publish: the id is immutable, and
          // re-sending it is refused with `package_id_immutable`.
          package_id: existing ? null : packageIdDraft.trim() || null,
        },
      }),
    onSuccess: (revision) => {
      setPublished(revision)
      // The projection now knows this skill has a package behind it, which
      // changes the row's verb from "Share…" to "Update published skill…", and
      // the catalog gained a package or a revision — both through the one
      // helper rather than a hand-listed key beside it.
      invalidateAddons(queryClient, agentId, { catalog: true })
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

  const title = isRepublish ? "Update published skill" : `Share ${skill.name}`
  const submitLabel = isRepublish ? "Update published skill" : "Share"

  const selectedPeople: UserAllowlistSelectedItem[] = people.map((person) => ({
    id: person.id,
    userId: person.id,
    fallbackLabel: person.label,
  }))

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
          <ShareSkillSuccessPanel
            skillName={skill.name}
            revision={published}
            packageHandle={handle}
            onClose={() => onOpenChange(false)}
          />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle className="flex min-w-0 items-center gap-2">
                <Upload className="h-5 w-5 shrink-0" />
                <span className="truncate">{title}</span>
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
                <Label htmlFor="share-skill-version">Version</Label>
                <Input
                  id="share-skill-version"
                  value={version}
                  disabled={isPending}
                  placeholder="1.0.0"
                  onChange={(e) => setVersion(e.target.value)}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="share-skill-notes">
                  Release notes{" "}
                  <span className="text-muted-foreground">(optional)</span>
                </Label>
                <Textarea
                  id="share-skill-notes"
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

              {isUsersVisibility && (
                <div className="space-y-2">
                  {/* Read-only on a republish: additions travel with this
                      publish, removals live on the package's own page beside
                      the sentence that says what removing costs. */}
                  {existingGrants.length > 0 && (
                    <div className="space-y-1.5">
                      <span className="text-xs text-muted-foreground">
                        Already shared with
                      </span>
                      <div className="flex flex-wrap gap-1.5">
                        {existingGrants.map((grant) => (
                          <span
                            key={grant.id}
                            className="rounded-full bg-muted px-2 py-1 text-xs text-muted-foreground"
                          >
                            {grant.user_email ?? "Unknown user"}
                          </span>
                        ))}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Managed on the package's catalog page.
                      </p>
                    </div>
                  )}

                  <UserAllowlistPicker
                    label={
                      <Label className="text-xs text-muted-foreground">
                        Add people
                      </Label>
                    }
                    selected={selectedPeople}
                    excludeUserIds={existingGrants.map(
                      (grant) => grant.user_id,
                    )}
                    isAdding={isPending}
                    isRemoving={isPending}
                    onAdd={(user) =>
                      setPeople((prev) =>
                        prev.some((person) => person.id === user.id)
                          ? prev
                          : [
                              ...prev,
                              {
                                id: user.id,
                                email: user.email,
                                label: user.full_name || user.email,
                              },
                            ],
                      )
                    }
                    onRemove={(item) =>
                      setPeople((prev) =>
                        prev.filter((person) => person.id !== item.id),
                      )
                    }
                    searchPlaceholder="Search people by name or email…"
                  />

                  {/* Not blocking (plan §9): a package with nobody on the list
                      is effectively private, which is a legitimate state to
                      publish into — but it is not what "People" sounds like. */}
                  {people.length === 0 && existingGrants.length === 0 && (
                    <Alert>
                      <AlertDescription>
                        Nobody can see it yet — add people here, or from the
                        package's catalog page later.
                      </AlertDescription>
                    </Alert>
                  )}
                </div>
              )}

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
                      <Label htmlFor="share-skill-package-id">Package id</Label>
                      <Input
                        id="share-skill-package-id"
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
                  offending paths, which is what this alert renders. It also
                  carries the grant refusals (`user_not_found`, `self_grant`):
                  a publish that names a bad address fails whole, so the reason
                  belongs beside the picker rather than in a toast. */}
              {publishMutation.isError && (
                <SkillCatalogErrorAlert
                  error={publishMutation.error}
                  fallback="Couldn't share the skill"
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
                {submitLabel}
              </LoadingButton>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
