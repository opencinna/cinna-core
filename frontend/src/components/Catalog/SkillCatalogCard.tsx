/**
 * SkillCatalogCard — one published skill package in the catalog grid.
 *
 * Six elements and no more, because the grid is `auto-rows-fr`: the tallest
 * card sets the height of every card in it, so anything whose length is data
 * driven is either clamped (`line-clamp-3`) or truncated. It is a browse tile,
 * not a concern card — the header shape is the sibling `CatalogCard`'s, which
 * is what makes the two sections of the catalog read as one page.
 */
import { useNavigate } from "@tanstack/react-router"
import { Download, Globe, GraduationCap, Lock } from "lucide-react"
import type { MouseEvent } from "react"
import { useState } from "react"

import type { SkillPackageEntry } from "@/client"
import { PublisherEmailConfirmedIcon } from "@/components/Common/PublisherEmailConfirmedIcon"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  skillPackageVersionLabel,
  skillPublisherLabel,
} from "@/utils/skillCatalog"
import { AddSkillToAgentDialog } from "./AddSkillToAgentDialog"

interface SkillCatalogCardProps {
  entry: SkillPackageEntry
}

export function SkillCatalogCard({ entry }: SkillCatalogCardProps) {
  const navigate = useNavigate()
  const [addOpen, setAddOpen] = useState(false)

  const installedCount = entry.installed_in_agent_ids?.length ?? 0
  const versionLabel = skillPackageVersionLabel(entry)
  const isPublic = entry.visibility === "public"

  const openDetail = () => {
    navigate({
      to: "/catalog/skills/$packageId",
      params: { packageId: entry.id },
    })
  }

  const handleAdd = (e: MouseEvent<HTMLButtonElement>) => {
    // The whole card is a navigation target; the footer button is the one
    // region inside it that means something else.
    e.stopPropagation()
    setAddOpen(true)
  }

  return (
    // The dialog is a **sibling** of the card, not a child of it: a React
    // portal still propagates events up the React tree, so a click inside a
    // dialog mounted under this `Card` would reach the card's `onClick` and
    // navigate away mid-form. Radix's `Dialog` root renders nothing inline, so
    // the grid still sees exactly one tile.
    <>
      <Card
        className="flex flex-col h-full cursor-pointer transition-colors hover:bg-accent/30 has-[button:hover]:bg-transparent has-[a:hover]:bg-transparent"
        onClick={openDetail}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          // Only when the card itself has focus. `keydown` bubbles from the
          // footer button before the browser synthesises its click, so an
          // unguarded handler would `preventDefault()` that click away and
          // navigate instead — leaving the one control on the card
          // keyboard-inoperable. `stopPropagation` on the button's `click`
          // cannot help: the click never happens.
          if (e.target !== e.currentTarget) return
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault()
            openDetail()
          }
        }}
      >
        <CardHeader className="pb-2">
          <div className="flex items-start gap-3">
            <div className="rounded-lg p-2 bg-muted shrink-0">
              <GraduationCap className="h-5 w-5 text-muted-foreground" />
            </div>
            <div className="flex-1 min-w-0">
              <CardTitle className="text-lg break-words leading-tight">
                {entry.display_name}
              </CardTitle>
              <p className="text-xs text-muted-foreground mt-1 truncate flex items-center gap-1">
                <span className="truncate">
                  by {skillPublisherLabel(entry)}
                </span>
                <PublisherEmailConfirmedIcon
                  confirmed={entry.publisher_email_confirmed ?? false}
                  hasEmail={!!entry.publisher_email}
                />
              </p>
            </div>
          </div>
        </CardHeader>

        <CardContent className="pt-0 flex-1 min-h-0 space-y-2">
          {entry.description && (
            <CardDescription className="line-clamp-3">
              {entry.description}
            </CardDescription>
          )}
          <div className="flex items-center gap-1.5 flex-wrap text-xs text-muted-foreground">
            <Badge variant="outline" className="gap-1 font-normal">
              {isPublic ? (
                <Globe className="h-3 w-3" />
              ) : (
                <Lock className="h-3 w-3" />
              )}
              {isPublic ? "public" : "private"}
            </Badge>
            {versionLabel && (
              <Badge variant="outline" className="font-normal">
                {versionLabel}
              </Badge>
            )}
          </div>
          <code
            className="block font-mono text-[11px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded truncate"
            title={entry.package_id}
          >
            {entry.package_id}
          </code>
          {/* Ids, not names: the list route returns `installed_in_agent_ids`, and
            a grid of cards cannot afford a name lookup per card. */}
          {installedCount > 0 && (
            <p className="text-xs text-muted-foreground">
              In {installedCount} of your agents
            </p>
          )}
        </CardContent>

        <CardFooter className="pt-2">
          <Button variant="outline" className="w-full" onClick={handleAdd}>
            <Download className="h-4 w-4 mr-2" />
            {installedCount > 0 ? "Add to another agent" : "Add to agent"}
          </Button>
        </CardFooter>
      </Card>

      {/* Mounted only while open: a resident dialog per card would run the
          agent list and the package detail query once per tile in the grid. */}
      {addOpen && (
        <AddSkillToAgentDialog
          packageId={entry.id}
          packageName={entry.display_name}
          open
          onOpenChange={setAddOpen}
        />
      )}
    </>
  )
}
