import type { ArtifactFileEntry } from "@/client"
import { JSONViewer } from "@/components/Environment/JSONViewer"
import { TextViewer } from "@/components/Environment/TextViewer"
import { formatBytes } from "../artifactFormat"
import { CsvTable } from "./CsvFileViewer"
import {
  basename,
  downloadUrl,
  fileUrl,
  previewKind,
  TEXT_PREVIEW_LIMIT,
} from "./fileKinds"
import { MarkdownFileViewer } from "./MarkdownFileViewer"
import { PdfFileViewer } from "./PdfFileViewer"
import {
  DownloadOnlyViewer,
  ImageFileViewer,
  SandboxedFrame,
} from "./SimpleFileViewers"
import { TextFileGate, ViewerMessage } from "./ViewerStates"

interface FilePreviewProps {
  contentBase: string
  file: ArtifactFileEntry
  paths: Set<string>
  onSelectFile: (path: string) => void
}

/** The file pane's body: one viewer per kind, scrolling inside the pane. */
export function FilePreview({
  contentBase,
  file,
  paths,
  onSelectFile,
}: FilePreviewProps) {
  const kind = previewKind(file)
  const name = basename(file.path)
  const url = fileUrl(contentBase, file.path)
  const dl = downloadUrl(contentBase, file.path)
  const textual =
    kind === "markdown" ||
    kind === "csv" ||
    kind === "tsv" ||
    kind === "json" ||
    kind === "text"

  if (file.size === 0 && kind !== "html") {
    return <ViewerMessage>This file is empty.</ViewerMessage>
  }
  if (textual && file.size > TEXT_PREVIEW_LIMIT) {
    return (
      <DownloadOnlyViewer
        name={name}
        path={file.path}
        size={file.size}
        href={dl}
        reason={`This file is ${formatBytes(file.size)} — too large to preview.`}
      />
    )
  }

  switch (kind) {
    case "markdown":
      return (
        <MarkdownFileViewer
          contentBase={contentBase}
          path={file.path}
          name={name}
          paths={paths}
          onSelectFile={onSelectFile}
        />
      )
    case "csv":
    case "tsv":
      return (
        <TextFileGate url={url} name={name}>
          {(text) => (
            <CsvTable
              text={text}
              delimiter={kind === "tsv" ? "\t" : ","}
              downloadHref={dl}
            />
          )}
        </TextFileGate>
      )
    case "json":
      return (
        <TextFileGate url={url} name={name}>
          {(text) => <JSONViewer content={text} />}
        </TextFileGate>
      )
    case "text":
      return (
        <TextFileGate url={url} name={name}>
          {(text) => <TextViewer content={text} />}
        </TextFileGate>
      )
    case "image":
      return <ImageFileViewer url={url} name={name} />
    case "pdf":
      return <PdfFileViewer url={url} name={name} />
    case "html":
      return <SandboxedFrame src={url} title={name} />
    default:
      return (
        <DownloadOnlyViewer
          name={name}
          path={file.path}
          size={file.size}
          href={dl}
        />
      )
  }
}
