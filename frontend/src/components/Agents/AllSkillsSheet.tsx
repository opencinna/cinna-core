import type { SkillEntryPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { skillKey } from "@/utils/skills"
import { SkillRow } from "./SkillRow"

interface AllSkillsSheetProps {
  agentId: string
  /** Every skill, already sorted the way the card sorts them. */
  skills: SkillEntryPublic[]
  /** The card's capability reply, passed straight through to the rows. */
  canPublish?: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Skills card.
 *
 * A Sheet rather than a route (P5's route-vs-Sheet test): the backend caps the
 * cached index at 200 entries (env-core's 50-skill budget is a *budget* — the
 * overflow stays in the list carrying `error: "budget"`), so the full list
 * needs no search, sort or pagination, and a skill has no lifecycle of its own
 * to give it a page. The rows are the card's rows — same component, same
 * actions — so the two hosts cannot drift.
 */
export function AllSkillsSheet({
  agentId,
  skills,
  canPublish,
  open,
  onOpenChange,
}: AllSkillsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Skills</SheetTitle>
          <SheetDescription>
            {skills.length} {skills.length === 1 ? "skill" : "skills"}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {skills.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              This agent has no skills yet.
            </p>
          ) : (
            <ListRowGroup>
              {skills.map((skill) => (
                <SkillRow
                  key={skillKey(skill)}
                  agentId={agentId}
                  skill={skill}
                  canPublish={canPublish}
                />
              ))}
            </ListRowGroup>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
