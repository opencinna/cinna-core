import type { ArtifactFileEntry } from "@/client"

export type PreviewKind =
  | "markdown"
  | "csv"
  | "tsv"
  | "json"
  | "text"
  | "image"
  | "pdf"
  | "html"
  | "other"

/** Text, JSON and CSV over this size are offered as a download only (plan D10). */
export const TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024

/** CSV/TSV rows rendered before "Showing N of M". */
export const CSV_ROW_LIMIT = 10_000

const TEXT_EXTENSIONS = new Set([
  "txt",
  "log",
  "py",
  "js",
  "mjs",
  "cjs",
  "ts",
  "tsx",
  "jsx",
  "css",
  "scss",
  "sql",
  "sh",
  "bash",
  "zsh",
  "yaml",
  "yml",
  "toml",
  "ini",
  "cfg",
  "conf",
  "xml",
  "env",
  "go",
  "rs",
  "rb",
  "java",
  "kt",
  "c",
  "h",
  "cpp",
  "hpp",
  "cs",
  "php",
  "swift",
  "r",
  "lua",
  "pl",
  "dockerfile",
  "makefile",
  "gitignore",
])

const IMAGE_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
  "avif",
  "bmp",
  "ico",
])

export function extensionOf(path: string): string {
  const name = path.split("/").pop() ?? path
  const dot = name.lastIndexOf(".")
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : name.toLowerCase()
}

export function basename(path: string): string {
  return path.split("/").pop() || path
}

/** Which viewer a manifest entry gets: extension first, the stored MIME second. */
export function previewKind(
  file: Pick<ArtifactFileEntry, "path" | "mime">,
): PreviewKind {
  const ext = extensionOf(file.path)
  const mime = (file.mime || "").split(";")[0].trim().toLowerCase()
  if (ext === "md" || ext === "markdown" || mime === "text/markdown")
    return "markdown"
  if (ext === "csv" || mime === "text/csv") return "csv"
  if (ext === "tsv" || mime === "text/tab-separated-values") return "tsv"
  if (ext === "json" || mime === "application/json") return "json"
  if (ext === "html" || ext === "htm" || mime === "text/html") return "html"
  if (ext === "pdf" || mime === "application/pdf") return "pdf"
  if (IMAGE_EXTENSIONS.has(ext) || mime.startsWith("image/")) return "image"
  if (TEXT_EXTENSIONS.has(ext) || mime.startsWith("text/")) return "text"
  return "other"
}

/** The file pane's default selection: README.md, else index.md, else the first. */
export function defaultFilePath(files: ArtifactFileEntry[]): string | null {
  const byLower = new Map(files.map((f) => [f.path.toLowerCase(), f.path]))
  return (
    byLower.get("readme.md") ??
    byLower.get("index.md") ??
    files[0]?.path ??
    null
  )
}

/**
 * A file's capability URL: `content_base` + the path, each segment escaped
 * (a `#` or `?` in a name must not end the path).
 */
export function fileUrl(contentBase: string, path: string): string {
  return contentBase + path.split("/").map(encodeURIComponent).join("/")
}

/** The same URL served as an attachment (D18). */
export function downloadUrl(contentBase: string, path: string): string {
  return `${fileUrl(contentBase, path)}?dl=1`
}

/** The inverse of `fileUrl` for a URL under `contentBase`, else null. */
export function pathFromUrl(contentBase: string, url: string): string | null {
  if (!url.startsWith(contentBase)) return null
  const rest = url.slice(contentBase.length).split(/[?#]/)[0]
  try {
    return rest.split("/").map(decodeURIComponent).join("/")
  } catch {
    return null
  }
}
