import { useQuery } from "@tanstack/react-query"
import { Copy, GraduationCap } from "lucide-react"
import { useState } from "react"

import type { SkillEntryPublic } from "@/client"
import { AgentsService } from "@/client"
import { SkillSource } from "@/components/Catalog/SkillSource"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import useCustomToast from "@/hooks/useCustomToast"

interface SkillContentDialogProps {
  agentId: string
  skill: SkillEntryPublic
  open: boolean
  onClose: () => void
}

/**
 * "Read the skill exactly as the model reads it" — S2.
 *
 * The **raw** file, frontmatter included, in a `<pre>`: rendering the markdown
 * would hide the frontmatter, reflow fenced blocks and add a viewer this
 * dialog does not need, and the stated intent is to show what the engine is
 * handed rather than a prettier version of it.
 *
 * Read-only, and a leaf: it opens nothing. Phase 3's Publish verb lives on the
 * row's `⋯` menu, so publishing stays card → dialog and never dialog → dialog
 * (guidelines §2 "Disclosure depth", A2).
 */
export function SkillContentDialog({
  agentId,
  skill,
  open,
  onClose,
}: SkillContentDialogProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isCopying, setIsCopying] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    // The key the plan specifies (§6). Note the coupling it buys: every caller
    // that invalidates `["agent", agentId]` — the prompt modals, the schedule
    // rows, the environment tab — prefix-matches this one too, and unlike the
    // index (cache-only, safe to poll) this route reads the container. The
    // dialog is modal, so that can only fire from a background event while it
    // is open, and a failed refetch keeps the shown text; if it ever turns out
    // to matter, move this key out from under `["agent", agentId]` and add it
    // to the refresh mutation's invalidation explicitly.
    queryKey: ["agent", agentId, "skills", skill.name, "content"],
    queryFn: () =>
      AgentsService.getAgentSkillContent({ agentId, name: skill.name }),
    // Belt and braces: the row mounts this only while open, so a resident
    // instance can never start fetching every skill in the list.
    enabled: open,
  })

  // Known before the request lands, so the path row does not pop in late.
  const expectedPath = `${skill.path ?? `skills/${skill.name}`}/SKILL.md`
  const path = data?.path ?? expectedPath

  // A local skill and a plugin skill can share a name — env-core keeps both
  // rows and flags `shadowed` on each — but the content route takes only the
  // name and resolves it local-first. So the shadowed row's View button gets
  // the *other* skill's body. Silently swapping the path row under the reader
  // would present it as this row's file; say which one arrived instead.
  const isShadowedByAnother = !!data && data.path !== expectedPath

  const copyBody = async () => {
    if (!data?.content) return
    setIsCopying(true)
    try {
      await navigator.clipboard.writeText(data.content)
      showSuccessToast("SKILL.md copied")
    } catch {
      // `navigator.clipboard` is undefined outside a secure context and
      // `writeText` rejects on a denied permission; unhandled, both leave a
      // button that visibly does nothing.
      showErrorToast("Failed to copy SKILL.md")
    } finally {
      setIsCopying(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 min-w-0">
            <GraduationCap className="h-5 w-5 shrink-0" />
            <span className="truncate">{skill.name}</span>
          </DialogTitle>
          <DialogDescription>
            {skill.description ||
              "This skill's SKILL.md, exactly as the model receives it."}
          </DialogDescription>
        </DialogHeader>

        <CopyableValue label="Path" value={path} />

        {/* The `<pre>`, its scroll cap and its four states are the shared
            `SkillSource` primitive's job — extracted when the package route's
            "SKILL.md" card became the second consumer of exactly this block. */}
        <SkillSource
          content={data?.content}
          truncated={data?.truncated}
          isLoading={isLoading}
          isError={isError && !data}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't load SKILL.md"
          notice={
            isShadowedByAnother ? (
              <p className="mb-2 text-xs text-warning">
                Another skill of this name shadows it — this is the text at{" "}
                <span className="font-mono">{data?.path}</span>, which is what
                the engine loads.
              </p>
            ) : null
          }
        />

        <DialogFooter>
          {/* Named for its effect, not for its verb: the path row above has a
              copy control of its own, so a button reading just "Copy" would
              not say which of the two it is. */}
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button onClick={copyBody} disabled={!data?.content || isCopying}>
            <Copy className="mr-2 h-4 w-4" />
            Copy SKILL.md
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
