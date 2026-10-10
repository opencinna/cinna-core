import { useMutation, useQuery } from "@tanstack/react-query"
import {
  ArrowDownToLine,
  Download,
  Files,
  GraduationCap,
  History,
  KeyRound,
  Loader2,
  Pencil,
} from "lucide-react"
import { useState } from "react"

import type { SkillPackageDetailPublic } from "@/client"
import { SkillsService } from "@/client"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { PublisherEmailConfirmedIcon } from "@/components/Common/PublisherEmailConfirmedIcon"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import {
  fetchAuthenticatedResponse,
  filenameFromResponse,
  getErrorMessage,
  saveBlobAs,
} from "@/utils"
import { catalogAccessFact } from "@/utils/catalogItems"
import {
  skillPackageVersionLabel,
  skillPublisherLabel,
  skillRevisionLabel,
} from "@/utils/skillCatalog"
import { requirementsSummary } from "@/utils/skillCredentials"
import { AddSkillToAgentDialog } from "./AddSkillToAgentDialog"
import { AllSkillRevisionsSheet } from "./AllSkillRevisionsSheet"
import { CatalogItemMenu } from "./CatalogItemMenu"
import { EditSkillPackageDialog } from "./EditSkillPackageDialog"
import { SkillRevisionCredentialsSheet } from "./SkillRevisionCredentialsSheet"
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
  const { showErrorToast } = useCustomToast()
  const [addOpen, setAddOpen] = useState(false)
  const [revisionsOpen, setRevisionsOpen] = useState(false)
  const [filesOpen, setFilesOpen] = useState(false)
  const [credentialsOpen, setCredentialsOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)

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
  // General access in the People dialog's words, a delisting on top — the
  // same fact the desktop agent's detail names "Access".
  const visibilityLabel = catalogAccessFact(pkg)
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
  // single number, which is why there is not one. Each value carries its unit
  // ("0 people", "3 agents"), which is what reconciles the two (§2 Fact labels).
  const catalogInstalls = pkg.install_count ?? 0
  const myInstalls = pkg.installed_in_agent_ids?.length ?? 0
  // What installing the *shown* revision needs — the pinned one, not the latest.
  const shownRequirements = shownRevision?.required_credentials ?? []
  const credentialsSummary = requirementsSummary(shownRequirements)
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
          {/* S3c: People…, Edit details…, the superuser's listing switch,
              Delete…. The menu renders nothing for a reader who may not
              manage the package (maintain, or a superuser — P2). */}
          <div className="shrink-0">
            <CatalogItemMenu
              item={{
                uuid: pkg.id,
                kind: "skill",
                runs_on: null,
                display_name: pkg.display_name,
                visibility: pkg.visibility,
                is_listed: pkg.is_listed,
              }}
              canMaintain={!!pkg.can_manage}
              extraItems={
                pkg.can_manage ? (
                  <DropdownMenuItem onSelect={() => setEditOpen(true)}>
                    <Pencil />
                    Edit details…
                  </DropdownMenuItem>
                ) : undefined
              }
            />
          </div>
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
          {/* What installing the shown revision needs. A door to the slot list
              like Version and Content — it costs no height — and, when the
              revision declares nothing, the absence is written: a fact list is
              where that answer belongs (§2 Absent facts). */}
          {shownRevision &&
            (credentialsSummary ? (
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-xs text-muted-foreground shrink-0">
                  Credentials
                </span>
                <button
                  type="button"
                  aria-haspopup="dialog"
                  aria-label={`${credentialsSummary}. Show every credential`}
                  onClick={() => setCredentialsOpen(true)}
                  className="flex min-w-0 items-center gap-1.5 rounded-sm text-sm font-medium underline-offset-4 outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"
                >
                  <span className="truncate">{credentialsSummary}</span>
                  <KeyRound className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                </button>
              </div>
            ) : (
              <Fact label="Credentials" value="None declared" />
            ))}
          {/* People, not agents, and never the publisher — so a publisher
              dogfooding their skill does not read this as adoption. */}
          <Fact
            label="Catalog installs"
            value={`${catalogInstalls} ${catalogInstalls === 1 ? "person" : "people"}`}
          />
          {myInstalls > 0 && (
            <Fact
              label="My agents"
              value={`${myInstalls} ${myInstalls === 1 ? "agent" : "agents"}`}
            />
          )}
          <Fact label="Access" value={visibilityLabel} />
        </div>

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

      <SkillRevisionCredentialsSheet
        requirements={shownRequirements}
        revisionLabel={shownVersionLabel}
        open={credentialsOpen}
        onOpenChange={setCredentialsOpen}
      />

      <AllSkillRevisionsSheet
        revisions={revisions}
        latestRevisionId={pkg.latest_revision_id}
        selectedRevisionNumber={selectedRevisionNumber}
        onView={onSelectRevision}
        open={revisionsOpen}
        onOpenChange={setRevisionsOpen}
      />
    </Card>
  )
}
