import { Link } from "@tanstack/react-router"
import {
  ExternalLink,
  FileText,
  GraduationCap,
  Puzzle,
  Upload,
} from "lucide-react"
import { useState } from "react"

import type { SkillEntryPublic } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { Button } from "@/components/ui/button"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { formatSkillSize, skillRowStatus } from "@/utils/skills"
import { PublishSkillDialog } from "./PublishSkillDialog"
import { SkillContentDialog } from "./SkillContentDialog"

interface SkillRowProps {
  agentId: string
  skill: SkillEntryPublic
  /**
   * The response-level capability: may this viewer publish *anything* from
   * this agent. A capability reply, never `useRole().isDeveloper` — a foreign
   * install degrades to use-only for every role, and only the server knows it.
   */
  canPublish?: boolean
}

/**
 * One skill, as a compact P3 row.
 *
 * Rendered by both the card and its "Show all" Sheet, so the two hosts cannot
 * drift, and — like `HandoverRow` — the row owns the dialog it opens: mounted
 * only while open, over the Sheet rather than instead of it.
 *
 * The metadata line is the skill's `description`, which is the one exception
 * this list makes to §2 "Row height": it is literally the text the model is
 * shown, and it is the only thing that tells two skills apart.
 *
 * The `⋯` beside the View button holds the row's non-View verbs, at most two:
 * "Publish to catalog…" — a Create action, which §1 "View" keeps out of a row's
 * visible controls — and, for a skill this agent did not author, the way to the
 * Plugins tab that installed it. Together that is 1 inline action + a menu of
 * ≤ 2, the row's whole budget.
 */
export function SkillRow({ agentId, skill, canPublish }: SkillRowProps) {
  const [contentOpen, setContentOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)

  const source = skill.source ?? "local"
  const path = skill.path ?? `skills/${skill.name}`

  // Two gates, both server-side. The response-level flag answers "may this
  // viewer publish from this agent at all"; without it the row shows no menu,
  // because a menu holding one permanently-disabled item is a dead control.
  // The entry-level flag folds in `source == "local"` **and** `is_publishable`
  // (no error, no secret hit), so the item is disabled rather than hidden when
  // the skill itself is the reason — "why can't I publish this one" is the
  // question here, and a missing item does not answer it. The reason is
  // already in the status dot's tooltip.
  const showPublish = !!canPublish && source === "local"
  // A skill that came from a plugin or the catalog is *managed* on the Plugins
  // tab — its version, its modes and its uninstall all live there — so the row
  // points at it rather than pretending this card owns it. It is also the
  // return leg of the trip the installed-plugin row's "Open in the skills
  // catalog" makes, which would otherwise be one-way.
  const showOpenInPlugins = source !== "local"
  const publishBlockedReason = skill.error
    ? "Fix the error before publishing"
    : skill.warning?.code === "secrets"
      ? "Remove the secrets before publishing"
      : null

  return (
    <ListRow
      // Unconditional, healthy rows included: `ListRow` draws the dot only
      // when it is given a status, so "ok = no colour" would indent every
      // clean row differently from every flagged one.
      status={skillRowStatus(skill)}
      title={skill.name}
      meta={skill.description}
      flags={
        <>
          {/* Local skills carry no source flag — the default needs no glyph. */}
          {source === "plugin" && (
            <RowFlag
              icon={Puzzle}
              label={
                skill.plugin_ref
                  ? `Comes from the plugin ${skill.plugin_ref}`
                  : "Comes from a plugin"
              }
            />
          )}
          {source === "catalog" && (
            <RowFlag
              icon={GraduationCap}
              label="Installed from the skills catalog"
            />
          )}
          {/* Everything true of the skill that nobody scans the list by. */}
          <RowInfo
            facts={[
              path,
              skill.has_scripts && "Ships scripts the agent can run",
              skill.user_invocable
                ? `Invocable from chat as /${skill.name}`
                : "Model-invoked only",
              formatSkillSize(skill.size_bytes),
            ]}
          />
        </>
      }
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label={`View SKILL.md for ${skill.name}`}
            // Frozen while the dialog it opened is up, so the triggering
            // control cannot stack a second one (§6 "Pending"). It is behind a
            // modal overlay meanwhile, so nothing is lost by disabling it.
            disabled={contentOpen}
            onClick={() => setContentOpen(true)}
          >
            <FileText className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          View SKILL.md
        </TooltipContent>
      </Tooltip>

      {(showPublish || showOpenInPlugins) && (
        <RowActionsMenu label={`the skill ${skill.name}`}>
          {showPublish && (
            <DropdownMenuItem
              disabled={!skill.can_publish}
              onSelect={(e) => {
                // The item unmounts on select and would take the dialog's own
                // pending state with it.
                e.preventDefault()
                setPublishOpen(true)
              }}
            >
              <Upload />
              {/* The reason replaces the verb rather than riding a tooltip: a
                  disabled Radix menu item swallows pointer events, so a
                  tooltip on it would never open. */}
              {publishBlockedReason ?? "Publish to catalog…"}
            </DropdownMenuItem>
          )}
          {showOpenInPlugins && (
            <DropdownMenuItem asChild>
              <Link to="/agent/$agentId" params={{ agentId }} hash="plugins">
                <ExternalLink />
                Open in Plugins tab
              </Link>
            </DropdownMenuItem>
          )}
        </RowActionsMenu>
      )}

      {/* Mounted only while open: a resident dialog would fetch every skill's
          body as soon as the card rendered. */}
      {contentOpen && (
        <SkillContentDialog
          agentId={agentId}
          skill={skill}
          open
          onClose={() => setContentOpen(false)}
        />
      )}

      {/* Owned by the row, like every confirm and editor a menu item opens: it
          must survive the item unmounting, and it must open *over* the "Show
          all" Sheet rather than instead of it. */}
      {publishOpen && (
        <PublishSkillDialog
          agentId={agentId}
          skill={skill}
          open
          onOpenChange={setPublishOpen}
        />
      )}
    </ListRow>
  )
}
