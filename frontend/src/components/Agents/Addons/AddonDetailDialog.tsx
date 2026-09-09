import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { formatDistanceToNow } from "date-fns"
import { Eye } from "lucide-react"
import { useState } from "react"

import type { AddonPublic, SkillEntryPublic } from "@/client"
import { SkillsService } from "@/client"
import { SkillContentBody } from "@/components/Agents/SkillContentBody"
import { ListRow, ListRowGroup, RowInfo } from "@/components/Common/ListRow"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  addonFormatLabel,
  addonIsFlagged,
  addonNoun,
  addonRowStatus,
  addonSourceFlag,
} from "@/utils/addons"
import { skillRevisionLabel } from "@/utils/skillCatalog"
import { formatSkillSize, skillKey, skillRowStatus } from "@/utils/skills"

interface AddonDetailDialogProps {
  agentId: string
  addon: AddonPublic
  open: boolean
  onOpenChange: (open: boolean) => void
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="shrink-0 text-xs text-muted-foreground">{label}</span>
      <span className="truncate text-sm">{value}</span>
    </div>
  )
}

/**
 * "What is this addon, where did it come from, what does it ship, and why is
 * it flagged" — S2, read-only.
 *
 * A **new composition**: no house pattern covers a read-only dialog whose body
 * is facts plus a swappable document pane. The problem it solves is that a
 * plugin can ship *n* skills, and showing them would otherwise mean either a
 * scroll of *n* documents or a second dialog on top of this one — and a dialog
 * does not open a dialog (guidelines R8). Swapping the pane in place is
 * read-only expand-in-place, which §2 allows; that is why `SkillContentBody`
 * exists as a block rather than as `SkillContentDialog`.
 *
 * Nothing here mutates. Every verb — enable, update, uninstall, share — stays
 * on the row that opened this (A2/R8).
 */
export function AddonDetailDialog({
  agentId,
  addon,
  open,
  onOpenChange,
}: AddonDetailDialogProps) {
  const skills = addon.skills ?? []
  const [selectedKey, setSelectedKey] = useState<string | null>(null)

  const link = addon.link ?? null
  const source = addonSourceFlag(addon)
  const SourceIcon = source.icon
  const status = addonRowStatus(addon)
  const noun = addonNoun(addon)

  // A catalog install pins a revision, and the revision *number* is what a
  // publisher and a consumer talk about — the link carries only its uuid, so
  // the package is read to turn one into the other. Catalog rows only.
  const packageId = link?.skill_package_id ?? null
  const { data: pkg } = useQuery({
    queryKey: ["skills-catalog", "package", packageId ?? "none"],
    queryFn: () =>
      SkillsService.getSkillPackage({ packageId: packageId ?? "" }),
    enabled: !!packageId,
  })
  const pinnedRevision = pkg?.revisions?.find(
    (rev) => rev.id === link?.skill_package_revision_id,
  )

  const selected: SkillEntryPublic | undefined =
    skills.length === 1
      ? skills[0]
      : (skills.find((skill) => skillKey(skill) === selectedKey) ?? skills[0])

  const modesLabel = link
    ? link.conversation_mode && link.building_mode
      ? "Conversation and building"
      : link.conversation_mode
        ? "Conversation only"
        : link.building_mode
          ? "Building only"
          : "No mode enabled"
    : null

  let installedLabel: string | null = null
  if (link) {
    try {
      installedLabel = formatDistanceToNow(new Date(link.created_at), {
        addSuffix: true,
      })
    } catch {
      installedLabel = null
    }
  }

  // A row that *is* one skill can put that skill's own facts in the fact list;
  // a plugin that ships several has nothing single to say and lists them below.
  const soleSkill = skills.length === 1 ? skills[0] : undefined

  // Resolved once: the helper narrows to `string | null`, so calling it twice
  // in the JSX left the second call needing a cast the first had already
  // earned.
  const formatLabel = addonFormatLabel(addon.plugin_type)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex min-w-0 items-center gap-2">
            <SourceIcon className="h-5 w-5 shrink-0" />
            <span className="truncate">{addon.display_name}</span>
          </DialogTitle>
          <DialogDescription>{source.label}</DialogDescription>
        </DialogHeader>

        {/* Block 2 — why it is flagged. Absent when the addon is healthy: an
            "all good" panel on every open would be the row's dot said twice. */}
        {addonIsFlagged(addon) && (
          <Alert variant={addon.status === "error" ? "destructive" : "default"}>
            <AlertDescription>
              {status.label}
              {addon.status_code === "orphan" && (
                <> Nothing on this agent installed it.</>
              )}
            </AlertDescription>
          </Alert>
        )}

        {/* Block 3 — the facts, in the two-column shape the package card uses
            so the two read as one product. */}
        <div className="space-y-1.5">
          {addon.description && (
            <p className="text-sm text-muted-foreground">{addon.description}</p>
          )}
          {addon.version && (
            <Fact label="Version" value={`v${addon.version}`} />
          )}
          {formatLabel && <Fact label="Format" value={formatLabel} />}
          {addon.marketplace_name && addon.source !== "local" && (
            <Fact label="Marketplace" value={addon.marketplace_name} />
          )}
          {pinnedRevision && (
            <Fact
              label="Pinned revision"
              value={skillRevisionLabel(pinnedRevision)}
            />
          )}
          {link?.installed_commit_hash && (
            <Fact label="Commit" value={link.installed_commit_hash} />
          )}
          {installedLabel && <Fact label="Installed" value={installedLabel} />}
          {modesLabel && <Fact label="Modes" value={modesLabel} />}
          {soleSkill?.path && <Fact label="Path" value={soleSkill.path} />}
          {soleSkill && (
            <Fact label="Size" value={formatSkillSize(soleSkill.size_bytes)} />
          )}
          {soleSkill && (
            <Fact
              label="Invocation"
              value={
                soleSkill.user_invocable
                  ? `From chat as /${soleSkill.name}`
                  : "Model-invoked only"
              }
            />
          )}
          {soleSkill?.has_scripts && (
            <Fact label="Scripts" value="Ships scripts the agent can run" />
          )}
          {/* Links onward rather than opening the catalog in a second dialog. */}
          {(addon.published_package_id ?? packageId) && (
            <Button asChild variant="link" className="h-auto px-0">
              <Link
                to="/catalog/skills/$packageId"
                params={{
                  packageId: (addon.published_package_id ??
                    packageId) as string,
                }}
              >
                Open in the skills catalog
              </Link>
            </Button>
          )}
        </div>

        {/* Block 4 — what it ships, and one of those skills' SKILL.md. */}
        {skills.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            This {noun} ships no skills.
          </p>
        ) : (
          <>
            {/* A data-driven list inside a dialog gets a max height, not a
                "Show all" (§2 "List inside a card", wizard-checklist clause).
                With one skill there is nothing to choose between, so the list
                collapses to the pane below it. */}
            {skills.length > 1 && (
              <div className="max-h-[40vh] overflow-y-auto">
                <ListRowGroup>
                  {skills.map((skill) => {
                    const key = skillKey(skill)
                    const isSelected = selected
                      ? skillKey(selected) === key
                      : false
                    return (
                      <ListRow
                        key={key}
                        status={skillRowStatus(skill)}
                        title={skill.name}
                        flags={
                          <RowInfo
                            facts={[
                              skill.description,
                              skill.path,
                              skill.has_scripts &&
                                "Ships scripts the agent can run",
                              skill.user_invocable
                                ? `Invocable from chat as /${skill.name}`
                                : "Model-invoked only",
                              formatSkillSize(skill.size_bytes),
                            ]}
                          />
                        }
                      >
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7"
                              aria-label={`Show the SKILL.md of ${skill.name}`}
                              aria-pressed={isSelected}
                              disabled={isSelected}
                              onClick={() => setSelectedKey(key)}
                            >
                              <Eye className="h-3.5 w-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            {isSelected ? "Shown below" : "Show this SKILL.md"}
                          </TooltipContent>
                        </Tooltip>
                      </ListRow>
                    )
                  })}
                </ListRowGroup>
              </div>
            )}

            {selected && (
              <SkillContentBody
                agentId={agentId}
                skill={selected}
                sourceClassName="max-h-[50vh]"
              />
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
