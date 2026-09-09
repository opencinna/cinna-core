import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { ChevronRight, MessageCircle, Wrench } from "lucide-react"
import { useState } from "react"

import type { AddonPublic, SkillEntryPublic } from "@/client"
import { SkillsService } from "@/client"
import { SkillContentBody } from "@/components/Agents/SkillContentBody"
import { ListRow, ListRowGroup, RowInfo } from "@/components/Common/ListRow"
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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import {
  addonFormatLabel,
  addonIsFlagged,
  addonNoun,
  addonRowStatus,
  addonSourceFlag,
} from "@/utils/addons"
import { skillRevisionLabel } from "@/utils/skillCatalog"
import { formatSkillSize, skillKey, skillRowStatus } from "@/utils/skills"
import { SkillDetailDialog } from "./SkillDetailDialog"

interface AddonDetailDialogProps {
  agentId: string
  addon: AddonPublic
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
 * "What is this addon, where did it come from, what does it ship, and why is
 * it flagged" — S2, read-only.
 *
 * A **new composition**: no house pattern covers a read-only dialog whose body
 * is facts plus a document. A row that *is* one skill (a catalog install, a
 * local folder, a `skills`-format entry) shows that skill's `SKILL.md` here,
 * rendered. A plugin that ships *n* skills lists them as rows instead, and a
 * row opens `SkillDetailDialog` — a dialog over this one, which R8 discourages
 * and which this used to avoid by swapping one skill's source into its own
 * body. That did not survive a plugin like chrome-devtools-mcp: a dozen rows
 * plus a document pane overflowed the viewport. So this dialog scrolls, and a
 * skill gets a dialog of its own.
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
  const [openSkillKey, setOpenSkillKey] = useState<string | null>(null)
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const copyCommit = async (hash: string | null | undefined) => {
    if (!hash) return
    try {
      await navigator.clipboard.writeText(hash)
      showSuccessToast("Commit hash copied")
    } catch {
      // `navigator.clipboard` is undefined outside a secure context and
      // `writeText` rejects on a denied permission; unhandled, both leave a
      // control that visibly does nothing.
      showErrorToast("Failed to copy the commit hash")
    }
  }

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

  const openSkill: SkillEntryPublic | undefined = skills.find(
    (skill) => skillKey(skill) === openSkillKey,
  )

  // The same two glyphs the Add addon modes step puts beside its checkboxes,
  // so the fact reads as the answer to that step's question.
  const conversation = (
    <span className="inline-flex items-center gap-1">
      <MessageCircle className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      Conversation
    </span>
  )
  const building = (
    <span className="inline-flex items-center gap-1">
      <Wrench className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      Building
    </span>
  )
  const modesValue = link ? (
    <span className="inline-flex items-center gap-1">
      {link.conversation_mode && link.building_mode ? (
        <>
          {conversation}
          <span>and</span>
          {building}
        </>
      ) : link.conversation_mode ? (
        <>
          {conversation}
          <span>only</span>
        </>
      ) : link.building_mode ? (
        <>
          {building}
          <span>only</span>
        </>
      ) : (
        "No mode enabled"
      )}
    </span>
  ) : null

  // A row that *is* one skill can put that skill's own facts in the fact list;
  // a plugin that ships several has nothing single to say and lists them below.
  const soleSkill = skills.length === 1 ? skills[0] : undefined

  // Resolved once: the helper narrows to `string | null`, so calling it twice
  // in the JSX left the second call needing a cast the first had already
  // earned.
  const formatLabel = addonFormatLabel(addon.plugin_type)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Capped and scrolling: a plugin's skill list is data-driven and the
          dialog must not grow past the viewport with it. */}
      <DialogContent
        className="max-h-[85vh] overflow-x-hidden overflow-y-auto sm:max-w-2xl [&>*]:min-w-0"
        // Radix moves focus to the first focusable thing on open, which is the
        // click-to-copy commit button — and a tooltip opens on focus, so the
        // dialog appeared with "Click to copy" showing over nothing the
        // pointer was near. Focus the body itself instead; Tab still reaches
        // every control in order.
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement | null)?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex min-w-0 items-center gap-2">
            <SourceIcon className="h-5 w-5 shrink-0" />
            <span className="truncate">{addon.display_name}</span>
          </DialogTitle>
          <DialogDescription>
            {source.label}
            {addon.author ? ` · by ${addon.author}` : ""}
          </DialogDescription>
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
            <p className="text-sm break-words text-muted-foreground">
              {addon.description}
            </p>
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
            <Fact
              label="Commit"
              // A hash exists to be pasted somewhere — into a `git log`, a bug
              // report — so the value itself is the copy control: hover says
              // so, click copies. No second input for it.
              value={
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      className="max-w-full cursor-pointer truncate rounded font-mono text-sm hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                      aria-label="Copy the commit hash"
                      onClick={() => copyCommit(link.installed_commit_hash)}
                    >
                      {link.installed_commit_hash}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="text-xs">
                    Click to copy
                  </TooltipContent>
                </Tooltip>
              }
            />
          )}
          {addon.repository_url && (
            <Fact
              label="Repository"
              value={
                <a
                  href={addon.repository_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block max-w-full truncate hover:underline"
                >
                  {addon.repository_url}
                </a>
              }
            />
          )}
          {link && (
            <Fact
              label="Installed"
              // The house component, not a bare `formatDistanceToNow`: the
              // server's naive-UTC string has to be parsed as UTC, and the
              // hover gives the full local date.
              value={<RelativeTime timestamp={link.created_at} showTooltip />}
            />
          )}
          {modesValue && <Fact label="Modes" value={modesValue} />}
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

        {/* Block 4 — what it ships. One skill: its SKILL.md, right here. Several:
            one row each, and the row opens the skill's own dialog. */}
        {skills.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            This {noun} ships no skills.
          </p>
        ) : soleSkill ? (
          <SkillContentBody agentId={agentId} skill={soleSkill} />
        ) : (
          <div>
            <p className="mb-1 text-xs text-muted-foreground">
              Ships {skills.length} skills — open one to read it.
            </p>
            <ListRowGroup>
              {skills.map((skill) => {
                const key = skillKey(skill)
                return (
                  // The whole row is the control, as the plugin's own row is
                  // on the Addons card: click, or Enter / Space, opens it.
                  // biome-ignore lint/a11y/useSemanticElements: `ListRow` renders `div`s, which a real `<button>` cannot contain.
                  <div
                    key={key}
                    role="button"
                    tabIndex={0}
                    aria-label={`Details of the skill ${skill.name}`}
                    className="min-w-0 cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                    onClick={() => setOpenSkillKey(key)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault()
                        setOpenSkillKey(key)
                      }
                    }}
                  >
                    <ListRow
                      status={skillRowStatus(skill)}
                      title={skill.name}
                      meta={skill.description}
                      flags={
                        <RowInfo
                          facts={[
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
                      <ChevronRight className="h-4 w-4 text-muted-foreground" />
                    </ListRow>
                  </div>
                )
              })}
            </ListRowGroup>
          </div>
        )}

        {openSkill && (
          <SkillDetailDialog
            agentId={agentId}
            skill={openSkill}
            partOf={`Part of the ${noun} ${addon.display_name}`}
            open
            onOpenChange={(next) => {
              if (!next) setOpenSkillKey(null)
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}
