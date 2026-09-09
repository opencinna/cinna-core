import { Link } from "@tanstack/react-router"
import { formatDistanceToNow } from "date-fns"
import {
  ArrowUpCircle,
  BookOpen,
  ExternalLink,
  Power,
  PowerOff,
  Trash2,
  Upload,
} from "lucide-react"
import { useState } from "react"

import type { AddonPublic } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { TooltipToggleItem } from "@/components/Common/TooltipToggleItem"
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
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { ToggleGroup } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  addonFormatLabel,
  addonNoun,
  addonRowStatus,
  addonSourceFlag,
} from "@/utils/addons"
import { formatSkillSize } from "@/utils/skills"
import { AddonDetailDialog } from "./AddonDetailDialog"
import { ShareSkillDialog } from "./ShareSkillDialog"
import { type SyncReporter, useAddonRowMutations } from "./useAddonRowMutations"

/**
 * The row's two mode segments. Short labels because the whole group must stay
 * ~130px so the name keeps its width at 1024 (§2 "Toggles on rows"); the full
 * sentence rides on the `aria-label` and the tooltip.
 */
const MODE_SEGMENTS = [
  {
    value: "conversation",
    short: "Chat",
    label: "Enabled in conversation mode",
  },
  { value: "building", short: "Build", label: "Enabled in building mode" },
]

export type { SyncReporter }

interface AddonRowProps {
  agentId: string
  addon: AddonPublic
  /** The response-level capability: may this viewer add or share here at all. */
  canAdd: boolean
  onSyncResult: SyncReporter
}

/**
 * One addon — a plugin or a skill, never both — as the plain P3 house row.
 *
 * Grown from `InstalledPluginRow` (whose mutations, mode toggle and confirm it
 * carries over) and `SkillRow` (whose status dot and detail facts it absorbs),
 * so the two lists the agent page used to show became one without either half
 * losing an affordance.
 *
 * **No `meta` line, on any row.** `SkillRow` put the skill's description there;
 * in a mixed list a metadata line on half the rows makes one list look like
 * two, which is the exact seam this projection exists to remove. Every
 * description goes to `RowInfo` — and that is why `SkillRow` is not reused
 * here rather than extended.
 *
 * Its three writes live in `useAddonRowMutations`, one observer set per row —
 * see that hook for why they are not shared with the card.
 */
export function AddonRow({
  agentId,
  addon,
  canAdd,
  onSyncResult,
}: AddonRowProps) {
  const [detailOpen, setDetailOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const link = addon.link ?? null
  const name = addon.display_name
  const noun = addonNoun(addon)
  const isBundle = addon.source === "bundle"
  const isLocal = addon.source === "local"
  const canManage = !!addon.can_manage && !!link
  const source = addonSourceFlag(addon)
  const skills = addon.skills ?? []

  const { update, upgrade, uninstall, isPending } = useAddonRowMutations(
    agentId,
    addon,
    onSyncResult,
    () => setConfirmOpen(false),
  )

  // The group is fully controlled off the server's two booleans, so Radix hands
  // back this exact array plus or minus exactly one entry — the caller wants
  // the one mode that moved, not the whole set.
  const modes = link
    ? [
        ...(link.conversation_mode ? ["conversation"] : []),
        ...(link.building_mode ? ["building"] : []),
      ]
    : []

  const installedAt = (() => {
    if (!link) return null
    try {
      return `Installed ${formatDistanceToNow(new Date(link.created_at), {
        addSuffix: true,
      })}`
    } catch {
      return null
    }
  })()

  const localSkill = isLocal ? skills[0] : undefined
  // A row that *is* one skill (local, or a catalog install) carries that
  // skill's own facts, exactly as `SkillRow` did — a plugin row that ships
  // several has nothing single to say here and sends the reader to Details.
  const soleSkill = skills.length === 1 ? skills[0] : undefined
  const shareBlockedReason = localSkill?.error
    ? "Fix the error before sharing"
    : localSkill?.warning?.code === "secrets"
      ? "Remove the secrets before sharing"
      : null

  // A local skill has no link, so `can_manage` is false on it by construction:
  // its whole budget is Details plus the share verb. `can_share` folds the
  // response-level capability and this skill's own cleanliness together, both
  // server-side — never `useRole()`, because a foreign install is use-only for
  // every role and only the server knows that.
  const showShare = isLocal && canAdd
  const shareLabel =
    addon.published_package_id != null ? "Update published skill…" : "Share…"

  const catalogPackageId = isLocal
    ? addon.published_package_id
    : link?.skill_package_id

  const menuItems: React.ReactNode[] = []
  if (canManage && link) {
    if (link.has_update && !isBundle) {
      menuItems.push(
        <DropdownMenuItem
          key="upgrade"
          onSelect={(e) => {
            e.preventDefault()
            upgrade.mutate()
          }}
        >
          <ArrowUpCircle />
          Update to v{link.latest_version ?? "latest"}
        </DropdownMenuItem>,
      )
    }
    menuItems.push(
      <DropdownMenuItem
        key="power"
        onSelect={(e) => {
          e.preventDefault()
          update.mutate({ disabled: !link.disabled })
        }}
      >
        {link.disabled ? <Power /> : <PowerOff />}
        {link.disabled ? "Enable" : "Disable"}
      </DropdownMenuItem>,
    )
  }
  if (showShare) {
    menuItems.push(
      <DropdownMenuItem
        key="share"
        // The reason replaces the verb rather than riding a tooltip: a disabled
        // Radix menu item swallows pointer events, so a tooltip on it would
        // never open. The reason is also already in the dot's tooltip.
        disabled={!addon.can_share}
        onSelect={(e) => {
          // The item unmounts on select and would take the dialog's own
          // pending state with it.
          e.preventDefault()
          setShareOpen(true)
        }}
      >
        <Upload />
        {/* The label is derived from the capability the button is disabled by,
            so a refusal this client has no sentence for still reads as a
            refusal instead of as an inert "Share…". */}
        {addon.can_share
          ? shareLabel
          : (shareBlockedReason ?? "It can't be shared as it is")}
      </DropdownMenuItem>,
    )
  }
  if (catalogPackageId) {
    menuItems.push(
      <DropdownMenuItem key="catalog" asChild>
        <Link
          to="/catalog/skills/$packageId"
          params={{ packageId: catalogPackageId }}
        >
          <ExternalLink />
          Open in the skills catalog
        </Link>
      </DropdownMenuItem>,
    )
  }
  // A bundle-sourced row has no uninstall of its own: it arrives and leaves
  // with the bundle's apply-update, which is what the `Package` flag says.
  // The server does not carry that half of the rule on `can_manage` — it is a
  // client-side read of `source`, exactly as the installed-plugin row did.
  if (canManage && !isBundle) {
    menuItems.push(
      <DropdownMenuSeparator key="sep" />,
      <DropdownMenuItem
        key="uninstall"
        variant="destructive"
        onSelect={(e) => {
          e.preventDefault()
          setConfirmOpen(true)
        }}
      >
        <Trash2 />
        Uninstall
      </DropdownMenuItem>,
    )
  }

  return (
    <ListRow
      // Disabled rows dim, and keep dimming when a warning outranks "off" on
      // the dot — which is how the disabled fact survives the precedence.
      muted={!!link?.disabled}
      status={addonRowStatus(addon)}
      title={name}
      // At most one badge. The version is the word people scan an install list
      // by; the source, the format and the description are flags or the detail
      // tooltip, which cost the name no width.
      badges={
        addon.version ? (
          <Badge variant="secondary" className="h-5">
            v{addon.version}
          </Badge>
        ) : undefined
      }
      flags={
        <>
          <RowFlag icon={source.icon} label={source.label} />
          {link?.has_update && (
            <RowFlag
              icon={ArrowUpCircle}
              tone="warning"
              label={`Update available — v${link.latest_version ?? "?"}`}
            />
          )}
          {addon.published_package_id && (
            <RowFlag icon={Upload} label="Published to the skills catalog" />
          )}
          <RowInfo
            facts={[
              addon.description,
              addonFormatLabel(addon.plugin_type),
              addon.marketplace_name && !isLocal
                ? `Marketplace ${addon.marketplace_name}`
                : null,
              installedAt,
              skills.length > 1 && `Ships ${skills.length} skills`,
              soleSkill?.path,
              soleSkill?.has_scripts && "Ships scripts the agent can run",
              soleSkill &&
                (soleSkill.user_invocable
                  ? `Invocable from chat as /${soleSkill.name}`
                  : "Model-invoked only"),
              soleSkill && formatSkillSize(soleSkill.size_bytes),
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
            aria-label={`Details of the ${noun} ${name}`}
            // Frozen while the dialog it opened is up, so the triggering
            // control cannot stack a second one (§6 "Pending"). It is behind a
            // modal overlay meanwhile, so nothing is lost by disabling it.
            disabled={detailOpen}
            onClick={() => setDetailOpen(true)}
          >
            <BookOpen className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Details
        </TooltipContent>
      </Tooltip>

      {canManage && link && (
        <ToggleGroup
          type="multiple"
          variant="outline"
          size="sm"
          // Only this row: the write is per-link, so freezing the rest of the
          // list for one round trip would be a cost with no matching risk.
          disabled={isPending || link.disabled}
          value={modes}
          onValueChange={(next) => {
            // An addon enabled in no mode is one that is on and does nothing:
            // the row would still read "Loaded" while the engine never loads
            // it, and the install step refuses the same state at the other
            // end. The backend has no such guard, so the floor is here — the
            // last segment cannot be turned off, and its tooltip says which
            // control does turn the addon off.
            if (next.length === 0) return
            const turnedOn = next.find((mode) => !modes.includes(mode))
            if (turnedOn) {
              update.mutate(
                turnedOn === "conversation"
                  ? { conversation_mode: true }
                  : { building_mode: true },
              )
              return
            }
            const turnedOff = modes.find((mode) => !next.includes(mode))
            if (turnedOff) {
              update.mutate(
                turnedOff === "conversation"
                  ? { conversation_mode: false }
                  : { building_mode: false },
              )
            }
          }}
          className="shrink-0"
          aria-label={`Modes ${name} is enabled in`}
        >
          {MODE_SEGMENTS.map((mode) => {
            const isOnlyMode = modes.length === 1 && modes[0] === mode.value
            return (
              <TooltipToggleItem
                key={mode.value}
                value={mode.value}
                label={mode.label}
                tooltip={
                  isOnlyMode
                    ? `${mode.label} — the only mode it runs in. Use Disable in the menu to turn it off.`
                    : mode.label
                }
                className="px-2"
              >
                {mode.short}
              </TooltipToggleItem>
            )
          })}
        </ToggleGroup>
      )}

      {/* A menu that would hold nothing is not rendered: an orphan row and a
          read-only foreign install both fall through to Details alone. */}
      {menuItems.length > 0 && (
        <RowActionsMenu label={`the ${noun} ${name}`} disabled={isPending}>
          {menuItems}
        </RowActionsMenu>
      )}

      {/* Every dialog and confirm a menu item opens is owned by the row, not
          nested in the `DropdownMenuItem`: the item unmounts on select and
          would take the thing's pending state with it. */}
      {detailOpen && (
        <AddonDetailDialog
          agentId={agentId}
          addon={addon}
          open
          onOpenChange={setDetailOpen}
        />
      )}

      {shareOpen && localSkill && (
        <ShareSkillDialog
          agentId={agentId}
          skill={localSkill}
          publishedPackageId={addon.published_package_id ?? null}
          open
          onOpenChange={setShareOpen}
        />
      )}

      {confirmOpen && (
        <AlertDialog
          open
          onOpenChange={(next) => {
            if (!uninstall.isPending) setConfirmOpen(next)
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Uninstall {name}?</AlertDialogTitle>
              <AlertDialogDescription>
                The {noun} is removed from this agent and from every environment
                it runs in. Nothing else on the agent changes, and you can add
                it again from Add addon.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={uninstall.isPending}>
                Cancel
              </AlertDialogCancel>
              <AlertDialogAction
                onClick={(e) => {
                  e.preventDefault()
                  uninstall.mutate()
                }}
                disabled={uninstall.isPending}
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                {uninstall.isPending ? "Uninstalling…" : "Uninstall"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </ListRow>
  )
}
