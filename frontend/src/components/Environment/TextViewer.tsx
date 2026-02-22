interface TextViewerProps {
  content: string
}

export function TextViewer({ content }: TextViewerProps) {
  if (!content.trim()) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">Empty file</p>
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto">
      <pre className="p-6 text-sm font-mono whitespace-pre-wrap break-words">{content}</pre>
    </div>
  )
}
