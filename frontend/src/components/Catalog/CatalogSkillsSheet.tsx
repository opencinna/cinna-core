import { Link } from "@tanstack/react-router"
import { GraduationCap } from "lucide-react"

import type { CatalogItemSkill } from "@/client"
import { ListRow, ListRowGroup, RowInfo } from "@/components/Common/ListRow"
import { Badge } from "@/components/ui/badge"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"

interface CatalogSkillsSheetProps {
  skills: CatalogItemSkill[]
  /** "v1.3" / "Version 4" of the version these skills ship in. */
  versionLabel: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * S2b — the skills an agent comes with, opened from its Skills fact.
 * Read-only rows. Authored is the default and renders nothing; a skill pinned
 * from the catalog says so and its name links to its own catalog page.
 */
export function CatalogSkillsSheet({
  skills,
  versionLabel,
  open,
  onOpenChange,
}: CatalogSkillsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Skills</SheetTitle>
          <SheetDescription>What {versionLabel} comes with.</SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {skills.map((skill) => (
              <ListRow
                key={`${skill.source}:${skill.name}`}
                icon={
                  <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
                    <GraduationCap className="h-3.5 w-3.5 text-muted-foreground" />
                  </span>
                }
                title={
                  skill.package_uuid ? (
                    <Link
                      to="/catalog/skills/$packageId"
                      params={{ packageId: skill.package_uuid }}
                      className="hover:underline"
                    >
                      {skill.name}
                    </Link>
                  ) : (
                    skill.name
                  )
                }
                badges={
                  skill.source === "catalog" ? (
                    <Badge variant="outline" className="h-5 font-normal">
                      From the catalog
                    </Badge>
                  ) : undefined
                }
                flags={<RowInfo facts={[skill.description]} />}
              />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}
