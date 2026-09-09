import { useQuery } from "@tanstack/react-query"
import { FileText } from "lucide-react"

import { SkillsService } from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
 * The same rendering as the agent-page viewer (S2), through the same
 * `SkillSource` component, so the catalog preview and the agent's own copy of a
 * skill cannot start rendering differently.
 */
export function SkillSourceCard({
  packageId,
  revisionNumber,
  revisionLabel,
}: SkillSourceCardProps) {
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

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <FileText className="h-5 w-5 shrink-0" />
          SKILL.md
        </CardTitle>
        <CardDescription>
          {revisionLabel
            ? `The published instructions of ${revisionLabel}.`
            : "The published instructions."}
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
