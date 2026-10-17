import {
  AppWindow,
  File as FileGlyph,
  Folder,
  type LucideIcon,
} from "lucide-react"

import type { ArtifactPublic } from "@/client"

/** What a source kind is called wherever an artifact names where it came from. */
const SOURCE_KIND_WORD: Record<string, string> = {
  desktop: "Desktop",
  core_agent: "Agent",
  cli: "CLI",
  upload: "Upload",
}

export function sourceKindWord(kind: string | null | undefined): string {
  return (kind && SOURCE_KIND_WORD[kind]) || "Upload"
}

/** The source line: the publisher's label, else the kind word (`||`: "" arrives). */
export function artifactSource(
  artifact: Pick<ArtifactPublic, "source_label" | "source_kind">,
): string {
  return artifact.source_label || sourceKindWord(artifact.source_kind)
}

export function artifactTitle(artifact: { title?: string | null }): string {
  return artifact.title || "Untitled artifact"
}

/** "Site" / "Files" — the Kind fact. */
export function artifactKindWord(kind: string): string {
  return kind === "site" ? "Site" : "Files"
}

/**
 * The row glyph: a site, a set of files, or the single file it is. One
 * function so the list, the page and the result panel cannot disagree.
 */
export function artifactKindIcon(
  artifact: Pick<ArtifactPublic, "kind" | "file_count">,
): LucideIcon {
  if (artifact.kind === "site") return AppWindow
  if ((artifact.file_count ?? 0) > 1) return Folder
  return FileGlyph
}

const UNITS = ["B", "KB", "MB", "GB"] as const

/** "4.2 MB" — one decimal under 10 of a unit, none above. */
export function formatBytes(bytes: number | null | undefined): string {
  let value = Math.max(bytes ?? 0, 0)
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1
  return `${value.toFixed(digits)} ${UNITS[unit]}`
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`
}
