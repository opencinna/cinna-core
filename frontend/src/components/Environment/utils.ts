import type { TreeItem } from "./types"

// API response types
export interface FileNode {
  type: "file" | "folder"
  name: string
  size?: number | null
  modified?: string | null
  children?: FileNode[]
}

// Helper function to format file size
export const formatFileSize = (bytes: number | null | undefined): string => {
  if (!bytes) return "0 B"
  const sizes = ["B", "KB", "MB", "GB"]
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${sizes[i]}`
}

// Helper function to format date
export const formatDate = (dateString: string | null | undefined): string => {
  if (!dateString) return ""
  const date = new Date(dateString)
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  })
}

// SQLite file extensions
const SQLITE_EXTENSIONS = [".db", ".sqlite", ".sqlite3"]

// Check if filename is a SQLite database
export const isSQLiteFile = (filename: string): boolean => {
  const lower = filename.toLowerCase()
  return SQLITE_EXTENSIONS.some(ext => lower.endsWith(ext))
}

// Helper function to get file extension from name
export const getFileExtension = (name: string): string => {
  // Check for SQLite files first (they use special handling)
  if (isSQLiteFile(name)) {
    return "sqlite"
  }
  const lastDot = name.lastIndexOf('.')
  return lastDot > 0 ? name.substring(lastDot + 1).toLowerCase() : 'txt'
}

// Convert API FileNode to UI TreeItem
export const convertFileNodeToTreeItem = (node: FileNode): TreeItem => {
  if (node.type === "folder") {
    return {
      type: "folder",
      name: node.name,
      size: node.size ? formatFileSize(node.size) : "0 B",
      modified: formatDate(node.modified),
      children: node.children ? node.children.map(convertFileNodeToTreeItem) : []
    }
  } else {
    return {
      type: "file",
      name: node.name,
      fileType: getFileExtension(node.name),
      size: formatFileSize(node.size),
      modified: formatDate(node.modified)
    }
  }
}