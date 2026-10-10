/**
 * CatalogItemCard — one catalog item (cloud agent, desktop agent or skill) in
 * the catalog grid. It replaces `CatalogCard` and `SkillCatalogCard` and keeps
 * their header skeleton: a muted glyph tile, `CardTitle text-lg`, "by …".
 *
 * Seven elements and no more, because the grid is `auto-rows-fr`: the tallest
 * tile sets the height of every tile, so anything data-driven is clamped or
 * truncated. The whole tile is the way into the item's detail; the footer
 * button is the one region inside it that means something else.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { GraduationCap, KeyRound } from "lucide-react"
import { useState } from "react"

import { type CatalogItem, InstallsService } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { useQuickInstall } from "@/components/Install/useQuickInstall"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  CATALOG_KINDS,
  catalogDetailTarget,
  catalogKindOf,
  catalogNeedsLine,
  catalogVersionLabel,
  myCloudInstall,
  myInstallLine,
} from "@/utils/catalogItems"
import { AddSkillToAgentDialog } from "./AddSkillToAgentDialog"
import { CatalogItemAction } from "./CatalogItemAction"

/** Names shown on the skills line before the "+N more" tail. */
const MAX_SKILL_NAMES = 3

interface CatalogItemCardProps {
  item: CatalogItem
}

export function CatalogItemCard({ item }: CatalogItemCardProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [addOpen, setAddOpen] = useState(false)

  const kind = catalogKindOf(item)
  const KindIcon = CATALOG_KINDS[kind].icon
  const cloudInstall = myCloudInstall(item)
  // Called for every kind (hooks are unconditional); only a cloud tile fires it.
  const quickInstall = useQuickInstall(item.id)

  const applyUpdate = useMutation({
    mutationFn: (agentId: string) => InstallsService.applyUpdate({ agentId }),
    onSuccess: () => {
      showSuccessToast("Update applied")
      queryClient.invalidateQueries({ queryKey: ["catalog-items"] })
      queryClient.invalidateQueries({ queryKey: ["catalog"] })
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to apply the update")),
  })

  const pending = quickInstall.isPending || applyUpdate.isPending

  const openAgent = () => {
    if (cloudInstall?.install_id) {
      navigate({
        to: "/agent/$agentId",
        params: { agentId: cloudInstall.install_id },
      })
    }
  }

  const openDetail = () => {
    // A disabled footer button no longer receives the click, so it would fall
    // through to the tile and navigate away mid-install.
    if (pending) return
    // An installed cloud agent opens the agent itself; everything else opens
    // its catalog detail.
    if (kind === "cloud" && cloudInstall?.install_id) openAgent()
    else navigate(catalogDetailTarget(item))
  }

  const versionLabel = item.latest ? catalogVersionLabel(item.latest) : null
  const needs = catalogNeedsLine(item.required_credential_specs)
  const skillNames =
    kind === "skill"
      ? []
      : (item.skills ?? []).map((s) => s.name).filter(Boolean)
  const installLine = myInstallLine(item)

  return (
    // The dialog is a sibling of the tile, not a child: a portal still bubbles
    // React events up the tree, so a click inside it would reach the tile's
    // `onClick` and navigate away mid-form.
    <>
      <Card
        className="flex h-full cursor-pointer flex-col transition-colors hover:bg-accent/30 has-[a:hover]:bg-transparent has-[button:hover]:bg-transparent"
        onClick={openDetail}
        role="button"
        tabIndex={0}
        aria-label={`${item.display_name} — ${CATALOG_KINDS[kind].word}`}
        onKeyDown={(e) => {
          // Only when the tile itself has focus: keydown bubbles from the
          // footer button before the browser synthesises its click.
          if (e.target !== e.currentTarget) return
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault()
            openDetail()
          }
        }}
      >
        <CardHeader className="pb-2">
          {/* `min-w-0`: the header is a CSS grid, whose children otherwise
              refuse to shrink below their content and push "by …" past the
              tile's edge instead of truncating it. */}
          <div className="flex min-w-0 items-start gap-3">
            <div className="shrink-0 rounded-lg bg-muted p-2">
              <KindIcon className="h-5 w-5 text-muted-foreground" />
            </div>
            <div className="min-w-0 flex-1">
              <CardTitle className="text-lg leading-tight break-words">
                {item.display_name}
              </CardTitle>
              <p className="mt-1 truncate text-xs text-muted-foreground">
                by {item.publisher?.display_name || "unknown publisher"}
              </p>
            </div>
          </div>
        </CardHeader>

        <CardContent className="min-h-0 flex-1 space-y-2 pt-0">
          {item.description && (
            <CardDescription className="line-clamp-3">
              {item.description}
            </CardDescription>
          )}
          {/* Agents: kind + version. Skills: version only — the glyph already
              says skill. Absent version renders nothing (§2 Absent facts). */}
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            {kind !== "skill" && (
              <Badge variant="outline" className="h-5 font-normal">
                {CATALOG_KINDS[kind].word}
              </Badge>
            )}
            {versionLabel && (
              <Badge variant="outline" className="h-5 font-normal">
                {versionLabel}
              </Badge>
            )}
            {item.latest && (
              <RelativeTime
                timestamp={item.latest.published_at}
                showTooltip
                className="text-xs text-muted-foreground"
              />
            )}
          </div>
          {needs && (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <KeyRound className="h-3 w-3 shrink-0" />
              <span className="truncate">Needs {needs}</span>
            </p>
          )}
          {skillNames.length > 0 && (
            <p className="flex items-center gap-1.5 truncate text-xs text-muted-foreground">
              <GraduationCap className="h-3 w-3 shrink-0" />
              <span className="truncate">
                {skillNames.slice(0, MAX_SKILL_NAMES).join(" · ")}
                {skillNames.length > MAX_SKILL_NAMES &&
                  ` +${skillNames.length - MAX_SKILL_NAMES} more`}
              </span>
            </p>
          )}
          {installLine && (
            <p className="truncate text-xs text-muted-foreground">
              {installLine}
            </p>
          )}
        </CardContent>

        <CardFooter className="pt-2">
          <CatalogItemAction
            item={item}
            writes={{
              installing: quickInstall.isPending,
              updating: applyUpdate.isPending,
              install: () => quickInstall.mutate(),
              update: () => {
                if (cloudInstall?.install_id)
                  applyUpdate.mutate(cloudInstall.install_id)
              },
              open: openAgent,
              addToAgent: () => setAddOpen(true),
            }}
          />
        </CardFooter>
      </Card>

      {/* Mounted only while open: a resident dialog per tile would run its
          queries once per tile in the grid. */}
      {addOpen && (
        <AddSkillToAgentDialog
          packageId={item.uuid}
          packageName={item.display_name}
          open
          onOpenChange={setAddOpen}
        />
      )}
    </>
  )
}
