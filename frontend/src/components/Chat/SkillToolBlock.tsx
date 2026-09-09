import { GraduationCap } from "lucide-react"

interface SkillToolBlockProps {
  skillName: string
  args?: string
  isCompact?: boolean
}

export function SkillToolBlock({
  skillName,
  args,
  isCompact = false,
}: SkillToolBlockProps) {
  if (isCompact) {
    return (
      <div className="inline-flex items-center gap-2 text-sm text-muted-foreground/80 mb-1">
        <GraduationCap className="h-3.5 w-3.5 flex-shrink-0" />
        <span>
          Loading skill{" "}
          <code className="font-mono bg-muted px-1 py-0.5 rounded text-xs">
            {skillName}
          </code>
        </span>
      </div>
    )
  }

  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <GraduationCap className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="text-foreground/90">
          Loading skill{" "}
          <code className="font-mono bg-slate-200 dark:bg-slate-700 px-1.5 py-0.5 rounded text-xs">
            {skillName}
          </code>
        </span>
        {args && (
          // Labelled: an unlabelled second line under "Loading skill X" reads
          // as part of the skill, not as what it was invoked with.
          <div className="mt-1 text-xs text-muted-foreground break-words whitespace-pre-wrap">
            <span className="font-medium">Arguments: </span>
            {args}
          </div>
        )}
      </div>
    </div>
  )
}
