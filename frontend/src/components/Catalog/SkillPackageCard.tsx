import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowDownToLine,
  Download,
  EyeOff,
  Files,
  GraduationCap,
  History,
  Loader2,
  Pencil,
} from "lucide-react"
import { useState } from "react"

import type { SkillPackageDetailPublic } from "@/client"
import { SkillsService } from "@/client"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { PublisherEmailConfirmedIcon } from "@/components/Common/PublisherEmailConfirmedIcon"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
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
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import {
  fetchAuthenticatedResponse,
  filenameFromResponse,
  getErrorMessage,
  saveBlobAs,
} from "@/utils"
import {
  SKILL_VISIBILITY_OPTIONS,
  skillPackageVersionLabel,
  skillPublisherLabel,
  skillRevisionLabel,
} from "@/utils/skillCatalog"
import { AddSkillToAgentDialog } from "./AddSkillToAgentDialog"
import { AllSkillRevisionsSheet } from "./AllSkillRevisionsSheet"
import { EditSkillPackageDialog } from "./EditSkillPackageDialog"
import { SkillRevisionFilesSheet } from "./SkillRevisionFilesSheet"

interface SkillPackageCardProps {
  pkg: SkillPackageDetailPublic
  /** The revision the SKILL.md panel is showing — what the Version fact names. */
  selectedRevisionNumber: number | null
  /** Pin a revision into that panel. Called from the revisions Sheet. */
  onSelectRevision: (revisionNumber: number) => void
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-muted-foreground shrink-0">{label}</span>
      <span className="text-sm font-medium truncate">{value}</span>
    </div>
  )
}

/**
 * "What is this package, and do I want it" — the left column of S7.
 *
 * Four blocks: the facts, the copyable id, the primary action, and the
 * publisher's `⋯`. The publisher's verbs are in that menu and nowhere else
 * (A5): Edit and Delist are exactly the actions §1 "View" says must not be
 * visible controls on a surface whose story is *read this and decide*.
 *
 * The Version fact is also the way into the history: it opens
 * `AllSkillRevisionsSheet` with every publish of the package. That history used
 * to be a third card in the right column, under a SKILL.md panel whose height is
 * the length of somebody's prose — so on any real skill it started below the
 * fold and stayed there. A version is what a reader looks at to ask "which one
 * am I reading", which makes it the right door, and it costs no height.
 */
export function SkillPackageCard({
  pkg,
  selectedRevisionNumber,
  onSelectRevision,
}: SkillPackageCardProps) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [addOpen, setAddOpen] = useState(false)
  const [revisionsOpen, setRevisionsOpen] = useState(false)
  const [filesOpen, setFilesOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [delistOpen, setDelistOpen] = useState(false)

  // Delisting is superuser-only server-side (`SkillCatalogService.delist`), and
  // no capability field reports it, so this is read off the account rather than
  // off the payload. Deliberately not `useRole().isAdmin`, which is also true
  // for the `admin` *role* without `is_superuser` — offering a verb the API
  // would refuse is worse than not offering it.
  const canDelist = !!user?.is_superuser && pkg.is_listed

  const delistMutation = useMutation({
    mutationFn: () => SkillsService.delistSkillPackage({ packageId: pkg.id }),
    onSuccess: () => {
      showSuccessToast("Package delisted")
      setDelistOpen(false)
      queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to delist the package")),
  })

  // What the shown revision ships. Its own read rather than a field on the
  // package: the file list is a walk of an immutable snapshot on disk, it
  // changes with the revision the reader has pinned, and the catalog grid —
  // which renders the same package as a card — must not pay a directory walk
  // per row for a number nobody reads there.
  const {
    data: files,
    isLoading: filesLoading,
    isError: filesError,
  } = useQuery({
    queryKey: [
      "skills-catalog",
      "package",
      pkg.id,
      "files",
      selectedRevisionNumber,
    ],
    queryFn: () =>
      SkillsService.listSkillPackageRevisionFiles({
        packageId: pkg.id,
        revisionNumber: selectedRevisionNumber as number,
      }),
    enabled: selectedRevisionNumber != null,
  })

  const downloadMutation = useMutation({
    mutationFn: async () => {
      // Not through the generated SDK: it parses every response as JSON, and
      // this one is a tarball. Same helpers as the knowledge-source export.
      const response = await fetchAuthenticatedResponse(
        `/api/v1/skills/packages/${pkg.id}/revisions/${selectedRevisionNumber}/download`,
      )
      saveBlobAs(
        await response.blob(),
        filenameFromResponse(
          response,
          `${pkg.name}-${selectedRevisionNumber}.tar.gz`,
        ),
      )
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to download this skill")),
  })

  const versionLabel = skillPackageVersionLabel(pkg) ?? "No revision yet"
  // Three visibilities since `users` shipped, and a delisting on top of any of
  // them. The old two-branch expression printed "Private" for a package shared
  // with named people — telling its publisher the opposite of what they did.
  const visibilityLabel = (() => {
    const base =
      SKILL_VISIBILITY_OPTIONS.find((option) => option.value === pkg.visibility)
        ?.label ?? "Private"
    return pkg.is_listed ? base : `${base}, delisted`
  })()
  const revisions = pkg.revisions ?? []
  // What the SKILL.md panel is showing, which is not always the latest — the
  // fact would otherwise say `v2` at somebody reading `v1`. No count beside it:
  // how many times a skill has been republished is not a fact anyone decides on,
  // and the Sheet this opens says it anyway.
  const shownRevision = revisions.find(
    (rev) => rev.revision_number === selectedRevisionNumber,
  )
  const shownVersionLabel = shownRevision
    ? skillRevisionLabel(shownRevision)
    : versionLabel
  // Two different questions, so two facts. `install_count` is **other people**,
  // one per person however many agents they use it in; `installed_in_agent_ids`
  // is the viewer's own agents. A publisher who has just installed their own
  // skill in three of their agents is 0 and 3 — true, and unreadable as a
  // single number, which is why there is not one.
  const catalogInstalls = pkg.install_count ?? 0
  const myInstalls = pkg.installed_in_agent_ids?.length ?? 0
  const fileCount = files?.count ?? 0
  const fileCountLabel = `${fileCount} file${fileCount === 1 ? "" : "s"}`
  // The whole archive, built server-side by its own endpoint — it does not walk
  // the listing above, so it still works when the listing failed. That is why
  // it survives into the unavailable state instead of disappearing with the
  // count.
  const downloadButton = (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6"
          aria-label={`Download ${shownVersionLabel} as a .tar.gz`}
          disabled={downloadMutation.isPending}
          onClick={() => downloadMutation.mutate()}
        >
          {downloadMutation.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <ArrowDownToLine className="h-3.5 w-3.5" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {downloadMutation.isPending
          ? "Preparing the download…"
          : `Download ${shownVersionLabel} as a .tar.gz`}
      </TooltipContent>
    </Tooltip>
  )

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <GraduationCap className="h-5 w-5 shrink-0" />
            Package
          </CardTitle>
          {(pkg.can_manage || canDelist) && (
            <div className="shrink-0">
              <RowActionsMenu
                label={`the package ${pkg.display_name}`}
                disabled={delistMutation.isPending}
              >
                {pkg.can_manage && (
                  <DropdownMenuItem
                    onSelect={(e) => {
                      // The item unmounts on select and would take the dialog's
                      // own pending state with it.
                      e.preventDefault()
                      setEditOpen(true)
                    }}
                  >
                    <Pencil />
                    Edit details…
                  </DropdownMenuItem>
                )}
                {pkg.can_manage && canDelist && <DropdownMenuSeparator />}
                {canDelist && (
                  <DropdownMenuItem
                    onSelect={(e) => {
                      e.preventDefault()
                      setDelistOpen(true)
                    }}
                  >
                    <EyeOff />
                    Delist from the catalog
                  </DropdownMenuItem>
                )}
              </RowActionsMenu>
            </div>
          )}
        </div>
        <CardDescription>
          {pkg.description || "This package has no description."}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-xs text-muted-foreground shrink-0">
              Publisher
            </span>
            <span className="flex min-w-0 items-center gap-1">
              <span className="truncate text-sm font-medium">
                {skillPublisherLabel(pkg)}
              </span>
              <PublisherEmailConfirmedIcon
                confirmed={pkg.publisher_email_confirmed ?? false}
                hasEmail={!!pkg.publisher_email}
              />
            </span>
          </div>
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-xs text-muted-foreground shrink-0">
              Version
            </span>
            {revisions.length > 0 ? (
              <button
                type="button"
                aria-haspopup="dialog"
                aria-label={`Version ${shownVersionLabel}. Show every revision`}
                onClick={() => setRevisionsOpen(true)}
                className="flex min-w-0 items-center gap-1.5 rounded-sm text-sm font-medium underline-offset-4 outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"
              >
                <span className="truncate">{shownVersionLabel}</span>
                <History className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              </button>
            ) : (
              <span className="text-sm font-medium truncate">
                {versionLabel}
              </span>
            )}
          </div>
          {/* The SKILL.md panel beside this card is one file of what a skill
              ships; a skill that carries `scripts/` and `references/` looked
              from here exactly like one that carries nothing. This is the rest
              of the answer at the density the decision needs — a count, the
              names behind it, and the folder itself.

              A failed read says so here rather than taking the line with it: a
              fact list is one of the two places §2 "Absent facts" allows an
              absence to be written, and a row that silently disappears is the
              one thing §6 forbids an error to look like. It cannot delegate the
              recovery to the SKILL.md panel's Retry either — that button
              refetches its own `content` observer and never touches this
              `files` query. The skeleton is still only for the in-flight read,
              and the error branch asks for there being nothing to show, so a
              failed background refetch does not blank a count already in
              hand. */}
          {selectedRevisionNumber != null && (
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-xs text-muted-foreground shrink-0">
                Content
              </span>
              {filesError && !files ? (
                <span className="flex min-w-0 items-center gap-1">
                  <span className="truncate text-sm font-medium text-muted-foreground">
                    Unavailable
                  </span>
                  {downloadButton}
                </span>
              ) : filesLoading || !files ? (
                <Skeleton className="h-4 w-20" />
              ) : (
                <span className="flex min-w-0 items-center gap-1">
                  <button
                    type="button"
                    aria-haspopup="dialog"
                    aria-label={`${fileCountLabel}. Show every file`}
                    onClick={() => setFilesOpen(true)}
                    className="flex min-w-0 items-center gap-1.5 rounded-sm text-sm font-medium underline-offset-4 outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"
                  >
                    <span className="truncate">{fileCountLabel}</span>
                    <Files className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  </button>
                  {downloadButton}
                </span>
              )}
            </div>
          )}
          {/* People, not agents, and never the publisher — so a publisher
              dogfooding their skill does not read this as adoption. */}
          <Fact label="Catalog installs" value={String(catalogInstalls)} />
          {myInstalls > 0 && (
            <Fact label="Used in my agents" value={String(myInstalls)} />
          )}
          <Fact label="Visibility" value={visibilityLabel} />
        </div>

        {/* Only for the publisher, and only when the two numbers disagree:
            "Installs 0" beside "In your agents 3" is a contradiction until
            somebody says which agents each one counts. */}
        {pkg.can_manage && myInstalls > 0 && (
          <p className="text-xs text-muted-foreground">
            Catalog installs counts other people, one each — your own agents are
            never counted, so dogfooding cannot inflate it.
          </p>
        )}

        <CopyableValue label="Package id" value={pkg.package_id} />

        <Button className="w-full" onClick={() => setAddOpen(true)}>
          <Download className="h-4 w-4 mr-2" />
          Add to agent
        </Button>
      </CardContent>

      {addOpen && (
        <AddSkillToAgentDialog
          packageId={pkg.id}
          packageName={pkg.display_name}
          open
          onOpenChange={setAddOpen}
        />
      )}
      {editOpen && (
        <EditSkillPackageDialog pkg={pkg} open onOpenChange={setEditOpen} />
      )}

      <SkillRevisionFilesSheet
        files={files?.data ?? []}
        count={files?.count ?? 0}
        totalSizeBytes={files?.total_size_bytes ?? 0}
        truncated={files?.truncated ?? false}
        revisionLabel={shownRevision ? shownVersionLabel : null}
        open={filesOpen}
        onOpenChange={setFilesOpen}
      />

      <AllSkillRevisionsSheet
        revisions={revisions}
        latestRevisionId={pkg.latest_revision_id}
        selectedRevisionNumber={selectedRevisionNumber}
        onView={onSelectRevision}
        open={revisionsOpen}
        onOpenChange={setRevisionsOpen}
      />

      {/* Owned by the card rather than nested in the menu item that opens it:
          a `DropdownMenuItem` unmounts on select and would take the confirm's
          pending state with it. Delisting is reversible by the publisher
          (`is_listed` on the edit dialog), so the confirm names the package and
          says what it does rather than warning about permanence. */}
      <AlertDialog
        open={delistOpen}
        onOpenChange={(next) => {
          if (!delistMutation.isPending) setDelistOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delist {pkg.display_name}?</AlertDialogTitle>
            <AlertDialogDescription>
              The package disappears from the skills catalog for everyone. It is
              not deleted, and agents that already installed it keep working —
              their link points at a revision, which delisting does not touch.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={delistMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                delistMutation.mutate()
              }}
              disabled={delistMutation.isPending}
            >
              {delistMutation.isPending ? "Delisting…" : "Delist package"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
