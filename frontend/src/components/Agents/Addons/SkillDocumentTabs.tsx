import { FileText, Info } from "lucide-react"
import type { ReactNode } from "react"

import type { SkillEntryPublic } from "@/client"
import { SkillContentBody } from "@/components/Agents/SkillContentBody"
import {
  SEGMENTED_DIALOG_PANEL_CLASS,
  SEGMENTED_LIST_CLASS,
  SEGMENTED_PANEL_FOCUS_CLASS,
  SEGMENTED_TRIGGER_CLASS,
} from "@/components/Common/SegmentedTabs"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

interface SkillDocumentTabsProps {
  agentId: string
  skill: SkillEntryPublic
  /** The dialog's flags, credentials and facts — everything but the document. */
  children: ReactNode
}

/**
 * A skill's detail dialog body as two tabs at the top: **Details**, then
 * **SKILL.md**.
 *
 * The dialog used to stack the facts and the rendered `SKILL.md` in one
 * scroll, so every open was a dozen fact lines over a page of prose, and the
 * facts had to be scrolled back to. The catalog's package route splits the
 * same two things into two cards side by side; a dialog has no second column,
 * so here they are two tabs.
 *
 * Radix unmounts the inactive tab, and that is load-bearing: the `SKILL.md`
 * read goes through the container and wakes a suspended agent, so it happens
 * when the reader asks for the document rather than on every open. The Content
 * fact in Details is read host-side and never wakes anything.
 *
 * Shared by `AddonDetailDialog` (a row that is one skill) and
 * `SkillDetailDialog` (one skill of a plugin).
 */
export function SkillDocumentTabs({
  agentId,
  skill,
  children,
}: SkillDocumentTabsProps) {
  return (
    <Tabs defaultValue="details" className="min-w-0 gap-4">
      <TabsList className={SEGMENTED_LIST_CLASS}>
        <TabsTrigger value="details" className={SEGMENTED_TRIGGER_CLASS}>
          <Info className="h-3.5 w-3.5" />
          Details
        </TabsTrigger>
        <TabsTrigger value="skill-md" className={SEGMENTED_TRIGGER_CLASS}>
          <FileText className="h-3.5 w-3.5" />
          SKILL.md
        </TabsTrigger>
      </TabsList>
      <TabsContent value="details" className={SEGMENTED_DIALOG_PANEL_CLASS}>
        {children}
      </TabsContent>
      <TabsContent
        value="skill-md"
        className={`min-w-0 ${SEGMENTED_PANEL_FOCUS_CLASS}`}
      >
        <SkillContentBody agentId={agentId} skill={skill} />
      </TabsContent>
    </Tabs>
  )
}
