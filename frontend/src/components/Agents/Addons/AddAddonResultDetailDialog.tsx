import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { GraduationCap, Store } from "lucide-react"

import { SkillsService } from "@/client"
import { SkillSource } from "@/components/Catalog/SkillSource"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { type AddAddonResult, addonFormatLabel } from "@/utils/addons"
import {
  skillPackageVersionLabel,
  skillPublisherLabel,
} from "@/utils/skillCatalog"

interface AddAddonResultDetailDialogProps {
  result: AddAddonResult
  open: boolean
  onOpenChange: (open: boolean) => void
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="shrink-0 text-xs text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-sm">{value}</span>
    </div>
  )
}

/**
 * "What is this, who made it, and what does it say" — for a result the agent
 * does **not** carry yet, read-only.
 *
 * The same shape as `AddonDetailDialog` (title with the source glyph, a fact
 * list, a document pane) so an entry reads the same before and after it is
 * installed. It is a separate component because the two answer different
 * questions from different payloads: the installed dialog reads the link and
 * the environment's skill index, this one reads what discovery or the catalog
 * returned. A marketplace entry has no `SKILL.md` to fetch before it is
 * installed — the manifest's skill summary is all the syncer kept — so only a
 * catalog package gets the source pane, from its latest revision.
 *
 * Opened from inside the Add addon dialog, which is a dialog over a dialog —
 * a deliberate exception to R8 at the user's request: the row's info tooltip
 * sat under the list's scrollbar, and the facts it carried want more room than
 * a tooltip anyway.
 */
export function AddAddonResultDetailDialog({
  result,
  open,
  onOpenChange,
}: AddAddonResultDetailDialogProps) {
  const Icon = result.kind === "skill" ? GraduationCap : Store
  const noun = result.kind === "skill" ? "skill" : "plugin"
  const plugin = result.plugin
  const pkg = result.package

  const revisionNumber = pkg?.latest_revision_number ?? null
  const {
    data: source,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    // The same key the catalog's own source card uses, so a package read
    // there is a cache hit here.
    queryKey: [
      "skills-catalog",
      "package",
      pkg?.id ?? "none",
      "content",
      revisionNumber,
    ],
    queryFn: () =>
      SkillsService.getSkillPackageRevisionContent({
        packageId: pkg?.id ?? "",
        revisionNumber: revisionNumber as number,
      }),
    enabled: !!pkg && revisionNumber != null,
  })

  const formatLabel = plugin ? addonFormatLabel(plugin.plugin_type) : null
  const skillSummary = plugin?.skill_summary ?? null
  const publishedAt =
    pkg?.latest_revision?.published_at ?? pkg?.updated_at ?? null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-x-hidden overflow-y-auto sm:max-w-2xl [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="flex min-w-0 items-center gap-2">
            <Icon className="h-5 w-5 shrink-0" />
            <span className="truncate">{result.name}</span>
          </DialogTitle>
          <DialogDescription>
            {result.origin ??
              (pkg ? "From the skills catalog" : "From a marketplace")}
          </DialogDescription>
        </DialogHeader>

        {!result.supported && result.unsupportedReason && (
          <Alert>
            <AlertDescription>{result.unsupportedReason}</AlertDescription>
          </Alert>
        )}

        <div className="space-y-1.5">
          {result.description && (
            <p className="text-sm break-words text-muted-foreground">
              {result.description}
            </p>
          )}
          {result.version && (
            <Fact label="Version" value={`v${result.version}`} />
          )}
          {formatLabel && <Fact label="Format" value={formatLabel} />}
          {plugin?.marketplace_name && (
            <Fact label="Marketplace" value={plugin.marketplace_name} />
          )}
          {plugin?.author_name && (
            <Fact label="Author" value={plugin.author_name} />
          )}
          {plugin?.author_email && (
            <Fact
              label="Author email"
              value={
                <a
                  href={`mailto:${plugin.author_email}`}
                  className="hover:underline"
                >
                  {plugin.author_email}
                </a>
              }
            />
          )}
          {plugin?.category && (
            <Fact label="Category" value={plugin.category} />
          )}
          {plugin?.homepage && (
            <Fact
              label="Homepage"
              value={
                <a
                  href={plugin.homepage}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block max-w-full truncate hover:underline"
                >
                  {plugin.homepage}
                </a>
              }
            />
          )}
          {plugin?.repository_url &&
            plugin.repository_url !== plugin.homepage && (
              <Fact
                label="Repository"
                value={
                  <a
                    href={plugin.repository_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block max-w-full truncate hover:underline"
                  >
                    {plugin.repository_url}
                  </a>
                }
              />
            )}
          {skillSummary && (
            <Fact
              label="Ships the skill"
              value={
                skillSummary.description
                  ? `${skillSummary.name} — ${skillSummary.description}`
                  : skillSummary.name
              }
            />
          )}
          {skillSummary?.has_scripts && (
            <Fact label="Scripts" value="Ships scripts the agent can run" />
          )}
          {pkg && <Fact label="Publisher" value={skillPublisherLabel(pkg)} />}
          {pkg && skillPackageVersionLabel(pkg) && (
            <Fact label="Latest" value={skillPackageVersionLabel(pkg)} />
          )}
          {pkg && publishedAt && (
            <Fact
              label="Published"
              value={<RelativeTime timestamp={publishedAt} showTooltip />}
            />
          )}
          {pkg && (pkg.install_count ?? 0) > 0 && (
            <Fact label="Installs" value={String(pkg.install_count)} />
          )}
          {pkg && (
            <Button asChild variant="link" className="h-auto px-0">
              <Link
                to="/catalog/skills/$packageId"
                params={{ packageId: pkg.id }}
              >
                Open in the skills catalog
              </Link>
            </Button>
          )}
        </div>

        {pkg &&
          (revisionNumber == null ? (
            <p className="text-sm text-muted-foreground">
              This {noun} has no published revision to show.
            </p>
          ) : (
            <SkillSource
              content={source?.content}
              truncated={source?.truncated}
              isLoading={isLoading}
              // Gated on there being nothing to show, so a failed background
              // refetch keeps the source on screen.
              isError={isError && !source}
              error={error}
              onRetry={() => refetch()}
              errorFallback="Couldn't load SKILL.md"
              className="max-h-[45vh]"
            />
          ))}
      </DialogContent>
    </Dialog>
  )
}
