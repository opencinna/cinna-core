import { Link } from "@tanstack/react-router"
import {
  ArrowUpCircle,
  ExternalLink,
  Loader2,
  MessageCircle,
  Power,
  PowerOff,
  Trash2,
  Upload,
  Wrench,
} from "lucide-react"
import { useState } from "react"

import type { AddonPublic } from "@/client"
import { ListRow, RowFlag } from "@/components/Common/ListRow"
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
import { Badge } from "@/components/ui/badge"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { addonNoun, addonRowStatus } from "@/utils/addons"
import { AddonLocalBadge, AddonPublishedFlag } from "./AddonBadges"
import { AddonDetailDialog } from "./AddonDetailDialog"
import { ShareSkillDialog } from "./ShareSkillDialog"
import { type SyncReporter, useAddonRowMutations } from "./useAddonRowMutations"

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
 * Grown from `InstalledPluginRow` (whose mutations and confirm it carries
 * over) and `SkillRow` (whose status dot and detail facts it absorbs), so the
 * two lists the agent page used to show became one without either half losing
 * an affordance. The per-mode toggle the plugin row had is **not** on this row:
 * the modes are read in Details and chosen at install time, and a list this
 * mixed has no room for a segmented control on every row.
 *
 * **No `meta` line, on any row.** `SkillRow` put the skill's description there;
 * in a mixed list a metadata line on half the rows makes one list look like
 * two, which is the exact seam this projection exists to remove. Every
 * description, source, format and install date lives in Details — and that is
 * why `SkillRow` is not reused here rather than extended.
 *
 * **The row is the Details control**, as the Add addon result row is the
 * selection control: click, or Enter / Space, opens the dialog. What is left
 * on the right is one tone-coloured flag (an update is available) and the `⋯`
 * menu, whose trigger stops the click so opening it never also opens Details.
 * The source glyph, the published glyph and the info tooltip are gone from the
 * row — the same shape the Add addon list has: name · version · author.
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
  const skills = addon.skills ?? []

  const { update, upgrade, uninstall, isPending } = useAddonRowMutations(
    agentId,
    addon,
    onSyncResult,
    () => setConfirmOpen(false),
  )

  // An update is *commit*-based: a marketplace entry with no version string
  // in its manifest (most of the official marketplace) still updates, so the
  // label must not depend on a version to print — and the wire value for "no
  // version" is `""` as often as `null`, which `??` would happily print as
  // "v".
  const latestVersion = link?.latest_version || null
  const updateLabel = latestVersion
    ? `Update to v${latestVersion}`
    : "Update to the latest"
  const updateFlagLabel = latestVersion
    ? `Update available — v${latestVersion}`
    : "Update available"

  const localSkill = isLocal ? skills[0] : undefined
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
          {updateLabel}
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

  // Only a click that lands in this row's own DOM opens Details. The menu's
  // content and every dialog below are portals, and React bubbles their
  // synthetic events up this tree regardless — without the `contains` check a
  // click inside the uninstall confirm would open Details under it.
  //
  // A click on the `⋯` trigger is the menu's, not the row's: it is the one
  // real button inside the row's DOM, so the guard is "did this start on a
  // button" rather than a wrapper that stops propagation.
  //
  // And not while a write is in flight: the disabled menu trigger no longer
  // receives the click, so it would land on the row and open Details on top
  // of an update the user is waiting for.
  const openDetails = (e: React.SyntheticEvent) => {
    if (isPending) return
    if (!e.currentTarget.contains(e.target as Node)) return
    if ((e.target as HTMLElement).closest("button")) return
    setDetailOpen(true)
  }

  // What the spinner stands for, so the wait has a name.
  const pendingLabel = upgrade.isPending
    ? "Updating…"
    : uninstall.isPending
      ? "Uninstalling…"
      : update.isPending
        ? "Saving…"
        : null

  return (
    // biome-ignore lint/a11y/useSemanticElements: `ListRow` renders `div`s, which a real `<button>` cannot contain — the same trade the catalog cards make.
    <div
      role="button"
      tabIndex={0}
      aria-label={`Details of the ${noun} ${name}`}
      className="cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      onClick={openDetails}
      onKeyDown={(e) => {
        if (e.key !== "Enter" && e.key !== " ") return
        if (isPending) return
        if (!e.currentTarget.contains(e.target as Node)) return
        // Only the row itself: Enter on the `⋯` trigger is the menu's.
        if (e.target !== e.currentTarget) return
        e.preventDefault()
        setDetailOpen(true)
      }}
    >
      <ListRow
        // Disabled rows dim, and keep dimming when a warning outranks "off" on
        // the dot — which is how the disabled fact survives the precedence.
        muted={!!link?.disabled}
        status={addonRowStatus(addon)}
        title={name}
        // The version people scan an install list by, and the author — in a
        // list that merges four sources, *who made it* is the fact that tells
        // two same-named entries apart. The same pair the Add addon list
        // shows, so an entry reads the same before and after it is installed.
        // `AddonBadges` adds the two words only a local row can say, and only
        // on a local row; no row carries the author badge *and* those.
        badges={
          <>
            {addon.version && (
              <Badge variant="secondary" className="h-5">
                v{addon.version}
              </Badge>
            )}
            {/* An absent version renders nothing, like every other absent fact
                on this row. It briefly rendered as a "No version" chip, which
                is the failure §2 already names for "Active": until a skill is
                published *every* local row would carry it — the most repeated
                and least informative word in the list, on the rows with the
                least other information. The publisher is not left guessing:
                the Share dialog fills the version in and says so, and Details
                states the absence in words. */}
            {/* Local rows only, and it lands in the slot `author` fills on
                every other source's row. */}
            <AddonLocalBadge addon={addon} />
            {/* Which modes it runs in, as the two glyphs the install step and
                Details use for them — read at a glance down the list, and the
                only place the row says anything about modes now that the
                toggle is gone. */}
            {link?.conversation_mode && (
              <RowFlag
                icon={MessageCircle}
                label="Enabled in conversation mode"
              />
            )}
            {link?.building_mode && (
              <RowFlag icon={Wrench} label="Enabled in building mode" />
            )}
            {addon.author && (
              <Badge variant="outline" className="h-5 max-w-[12rem]">
                <span className="truncate">{addon.author}</span>
              </Badge>
            )}
          </>
        }
        flags={
          <>
            {/* "It is in the catalog" is a passive per-row fact the user reads
                and cannot click, so it is a glyph here rather than a third
                badge on the contested title line. */}
            <AddonPublishedFlag addon={addon} />
            {link?.has_update && (
              <RowFlag
                icon={ArrowUpCircle}
                tone="warning"
                label={updateFlagLabel}
              />
            )}
          </>
        }
      >
        {/* A write in flight takes the menu's slot: a spinner where the `⋯`
            was, named by its tooltip, so an update that runs a container
            sync is visibly happening rather than a greyed-out trigger. */}
        {pendingLabel ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className="flex h-7 w-7 items-center justify-center"
                aria-live="polite"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                <span className="sr-only">{pendingLabel}</span>
              </span>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              {pendingLabel}
            </TooltipContent>
          </Tooltip>
        ) : (
          // A menu that would hold nothing is not rendered: an orphan row and
          // a read-only foreign install both fall through to Details alone.
          menuItems.length > 0 && (
            <RowActionsMenu label={`the ${noun} ${name}`}>
              {menuItems}
            </RowActionsMenu>
          )
        )}
      </ListRow>

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
    </div>
  )
}
