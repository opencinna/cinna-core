import { FileCode, FileText, Terminal } from "lucide-react"

import type { SkillRevisionFilePublic } from "@/client"
import { ListRow, ListRowGroup, RowFlag } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { formatSkillBytes } from "@/utils/skillCatalog"

interface SkillRevisionFilesSheetProps {
  /** Every file of the shown revision, in archive order. */
  files: SkillRevisionFilePublic[]
  /** Files in the snapshot — larger than `files.length` when truncated. */
  count: number
  totalSizeBytes: number
  /** True when the snapshot holds more files than the server listed. */
  truncated: boolean
  /** How the shown revision is named, for the description line. */
  revisionLabel: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "What else is in this skill" — the destination of the Package card's
 * Content fact.
 *
 * A Sheet rather than a route, by P5's route-vs-Sheet test: the list needs no
 * search, sort or pagination, and a file inside an immutable snapshot has no
 * lifecycle of its own — there is nothing to do to one. It is deliberately a
 * list of *names*, not a file browser: the catalog's job is to let a reader
 * decide whether to install a skill, and "it ships three scripts and two
 * references" answers that, while a per-file viewer would make the catalog a
 * general reader over other people's published trees.
 *
 * The one action over this content is the card's download, which takes the
 * whole folder — the same archive an agent installs, so what a reader
 * inspects is what an agent would run.
 */
export function SkillRevisionFilesSheet({
  files,
  count,
  totalSizeBytes,
  truncated,
  revisionLabel,
  open,
  onOpenChange,
}: SkillRevisionFilesSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Content</SheetTitle>
          <SheetDescription>
            {count} file{count === 1 ? "" : "s"} ·{" "}
            {formatSkillBytes(totalSizeBytes)}
            {revisionLabel ? ` in ${revisionLabel}` : ""}
            {truncated ? `, showing the first ${files.length}` : ""}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {files.map((file) => (
              <SkillFileRow key={file.path} file={file} />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}

/**
 * One published file.
 *
 * The folder prefix is muted so a list of `references/a.md`, `references/b.md`
 * reads as a tree without being one — the name is what the eye is looking for,
 * and indenting instead would cost the name width it needs at 1024.
 *
 * Which is also why the *prefix* is what truncates: `ListRow` truncates the
 * whole title, so a long `references/deep/nested/` would ellipse away the file
 * name — the one thing this row exists to show. The name is `shrink-0` and the
 * folder gives up its width first.
 */
function SkillFileRow({ file }: { file: SkillRevisionFilePublic }) {
  const cut = file.path.lastIndexOf("/")
  const dir = cut === -1 ? "" : file.path.slice(0, cut + 1)
  const base = cut === -1 ? file.path : file.path.slice(cut + 1)

  return (
    <ListRow
      icon={<FileIcon path={file.path} />}
      title={
        <span className="flex min-w-0 items-baseline">
          {dir && (
            <span className="min-w-0 truncate font-normal text-muted-foreground">
              {dir}
            </span>
          )}
          <span className="shrink-0">{base}</span>
        </span>
      }
      // Size, and only size: it is the one fact that tells a 200-byte stub
      // apart from the reference the skill actually leans on. Same shape as
      // `SkillRevisionRow`, so the two lists in this route read alike.
      meta={formatSkillBytes(file.size_bytes)}
      flags={
        file.is_executable ? (
          <RowFlag
            icon={Terminal}
            label="Executable — the archive keeps this file's run bit"
          />
        ) : undefined
      }
    />
  )
}

/**
 * Identity, not state: the glyph says what kind of file this is, and the fact
 * that it is executable is the `Terminal` flag on the right — `ListRow`'s
 * `icon` is documented as "not a place for state".
 */
function FileIcon({ path }: { path: string }) {
  const Icon = /\.(md|markdown|txt)$/i.test(path) ? FileText : FileCode
  return <Icon className="h-4 w-4 text-muted-foreground" />
}
