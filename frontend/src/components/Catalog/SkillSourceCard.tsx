import { useQuery } from "@tanstack/react-query"
import { Copy, FileText } from "lucide-react"
import { useState } from "react"

import { SkillsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { SkillSource } from "./SkillSource"

interface SkillSourceCardProps {
  packageId: string
  /** The revision whose source is shown. `null` while the package has none. */
  revisionNumber: number | null
  /** How the shown revision is named, for the description line. */
  revisionLabel: string | null
}

/**
 * "What does this skill actually say" — the second card of S7.
 *
 * The same raw-source decision as the agent-page viewer (S2), through the same
 * `SkillSource` component, so the catalog preview and the agent's own copy of a
 * skill cannot start rendering differently.
 */
export function SkillSourceCard({
  packageId,
  revisionNumber,
  revisionLabel,
}: SkillSourceCardProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isCopying, setIsCopying] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: [
      "skills-catalog",
      "package",
      packageId,
      "content",
      revisionNumber,
    ],
    queryFn: () =>
      SkillsService.getSkillPackageRevisionContent({
        packageId,
        revisionNumber: revisionNumber as number,
      }),
    enabled: revisionNumber != null,
  })

  const copy = async () => {
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
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <FileText className="h-5 w-5 shrink-0" />
            SKILL.md
          </CardTitle>
          <div className="shrink-0">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  aria-label="Copy SKILL.md"
                  disabled={!data?.content || isCopying}
                  onClick={copy}
                >
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                Copy SKILL.md
              </TooltipContent>
            </Tooltip>
          </div>
        </div>
        <CardDescription>
          {revisionLabel
            ? `The published source of ${revisionLabel}, exactly as the model receives it.`
            : "The published source, exactly as the model receives it."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {revisionNumber == null ? (
          <p className="text-sm text-muted-foreground">
            This package has no published revision to show.
          </p>
        ) : (
          <SkillSource
            content={data?.content}
            truncated={data?.truncated}
            isLoading={isLoading}
            // Gated on there being nothing to show, so a failed background
            // refetch keeps the source on screen.
            isError={isError && !data}
            error={error}
            onRetry={() => refetch()}
            errorFallback="Couldn't load SKILL.md"
          />
        )}
      </CardContent>
    </Card>
  )
}
