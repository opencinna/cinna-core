import { FileText, FileJson, FileCode, ScrollText, FileSpreadsheet, BookOpen, Database } from "lucide-react"

interface FileIconProps {
  fileType: string
  className?: string
}

export function FileIcon({ fileType, className = "h-4 w-4" }: FileIconProps) {
  switch (fileType) {
    case "csv":
      return <FileSpreadsheet className={`${className} text-green-500`} />
    case "json":
      return <FileJson className={`${className} text-blue-500`} />
    case "txt":
      return <FileText className={`${className} text-gray-500`} />
    case "py":
      return <FileCode className={`${className} text-purple-500`} />
    case "log":
      return <ScrollText className={`${className} text-yellow-500`} />
    case "md":
      return <BookOpen className={`${className} text-indigo-500`} />
    case "sqlite":
      return <Database className={`${className} text-orange-500`} />
    default:
      return <FileText className={className} />
  }
}
