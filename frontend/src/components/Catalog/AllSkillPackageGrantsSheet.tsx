import type { SkillPackageAccessGrantPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { SkillPackageGrantRow } from "./SkillPackageGrantRow"

interface AllSkillPackageGrantsSheetProps {
  packageId: string
  grants: SkillPackageAccessGrantPublic[]
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the access card.
 *
 * A Sheet rather than a route (P5's route-vs-Sheet test): an allowlist needs no
 * search, sort or pagination, and a grant has no lifecycle of its own — it
 * exists and then it does not. Same row component as the card, so the two
 * hosts cannot drift.
 */
export function AllSkillPackageGrantsSheet({
  packageId,
  grants,
  open,
  onOpenChange,
}: AllSkillPackageGrantsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Who can see this</SheetTitle>
          <SheetDescription>
            {grants.length} {grants.length === 1 ? "person" : "people"}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {grants.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No one has been added yet.
            </p>
          ) : (
            <ListRowGroup>
              {grants.map((grant) => (
                <SkillPackageGrantRow
                  key={grant.id}
                  packageId={packageId}
                  grant={grant}
                />
              ))}
            </ListRowGroup>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
