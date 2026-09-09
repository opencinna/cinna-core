import { useQuery } from "@tanstack/react-query"
import { Copy } from "lucide-react"
import { useState } from "react"

import type { SkillEntryPublic } from "@/client"
import { AgentsService } from "@/client"
import { SkillSource } from "@/components/Catalog/SkillSource"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"

interface SkillContentBodyProps {
  agentId: string
  skill: SkillEntryPublic
  /** Wrapper classes. */
  className?: string
  /** Height cap for the source pane; the host decides how much room it has. */
  sourceClassName?: string
}

/**
 * One skill's `SKILL.md`, with its path, exactly as the model receives it.
 *
 * Extracted from `SkillContentDialog`, whose whole body this was, when the
 * addon detail dialog became its consumer: S2 must show *n* skills and one
 * `SKILL.md` **without** opening a second dialog (guidelines R8), so the
 * document pane has to be a block it can swap in place rather than a dialog it
 * can stack. Keeping the fetch, the shadowing note and the copy affordance
 * here is what stops the two readers from drifting apart.
 *
 * It owns its own query, keyed per skill, so swapping the selected skill is a
 * cache read after the first look rather than a refetch.
 */
export function SkillContentBody({
  agentId,
  skill,
  className,
  sourceClassName,
}: SkillContentBodyProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isCopying, setIsCopying] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    // Nested under `["agent", agentId, "skills"]` on purpose: every path that
    // re-reads the environment invalidates that prefix, and this body was read
    // from the same container.
    queryKey: ["agent", agentId, "skills", skill.name, "content"],
    queryFn: () =>
      AgentsService.getAgentSkillContent({ agentId, name: skill.name }),
  })

  // Known before the request lands, so the path row does not pop in late.
  const expectedPath = `${skill.path ?? `skills/${skill.name}`}/SKILL.md`
  const path = data?.path ?? expectedPath

  // A local skill and a plugin skill can share a name — env-core keeps both
  // rows and flags `shadowed` on each — but the content route takes only the
  // name and resolves it local-first. So a shadowed entry's viewer gets the
  // *other* skill's body. Silently swapping the path row under the reader
  // would present it as this entry's file; say which one arrived instead.
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
    <div className={className}>
      <div className="flex items-end gap-2">
        <div className="min-w-0 flex-1">
          <CopyableValue label="Path" value={path} />
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8 shrink-0"
              aria-label="Copy SKILL.md"
              disabled={!data?.content || isCopying}
              onClick={copyBody}
            >
              <Copy className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Copy SKILL.md
          </TooltipContent>
        </Tooltip>
      </div>

      {/* The `<pre>`, its scroll cap and its four states are the shared
          `SkillSource` primitive's job. */}
      <div className="mt-2">
        <SkillSource
          content={data?.content}
          truncated={data?.truncated}
          isLoading={isLoading}
          // A failed *background* refetch keeps the text that is on screen; a
          // first failed load renders as a failure, never as an empty file.
          isError={isError && !data}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't load SKILL.md"
          className={sourceClassName}
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
      </div>
    </div>
  )
}
