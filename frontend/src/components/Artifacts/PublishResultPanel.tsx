import { Link } from "@tanstack/react-router"

import type { ArtifactPublished } from "@/client"
import { Button } from "@/components/ui/button"
import { DialogFooter } from "@/components/ui/dialog"
import { artifactKindWord, formatBytes, plural } from "./artifactFormat"

interface PublishResultPanelProps {
  result: ArtifactPublished
  onClose: () => void
}

/**
 * What a publish did, shown only when some files were left out (S3, and S10
 * in P6): the facts, the excluded paths, and the way onward to the artifact.
 */
export function PublishResultPanel({
  result,
  onClose,
}: PublishResultPanelProps) {
  const excluded = result.excluded ?? []
  const facts: [string, string][] = [
    ["Kind", artifactKindWord(result.kind)],
    ["Files", plural(result.file_count ?? 0, "file")],
    ["Size", formatBytes(result.size_bytes)],
  ]

  return (
    <>
      <div className="space-y-4">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          {facts.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-xs text-muted-foreground leading-5">
                {label}
              </dt>
              <dd className="text-sm">{value}</dd>
            </div>
          ))}
        </dl>
        <div className="space-y-1.5">
          <p className="text-sm font-medium">
            {plural(excluded.length, "file")}{" "}
            {excluded.length === 1 ? "was" : "were"} left out
          </p>
          <ul className="max-h-[40vh] overflow-y-auto pr-2 font-mono text-xs text-muted-foreground space-y-0.5">
            {excluded.map((path) => (
              <li key={path} className="break-all">
                {path}
              </li>
            ))}
          </ul>
        </div>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          Close
        </Button>
        <Button asChild>
          <Link
            to="/artifact/$artifactId"
            params={{ artifactId: result.id }}
            onClick={onClose}
          >
            Open artifact
          </Link>
        </Button>
      </DialogFooter>
    </>
  )
}
