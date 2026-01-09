import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

interface MarkdownViewerProps {
  content: string
}

export function MarkdownViewer({ content }: MarkdownViewerProps) {
  return (
    <div className="h-full overflow-auto p-6">
      <article className="prose prose-slate dark:prose-invert max-w-none">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            code(props) {
              const { className, children, ...rest } = props as any
              const match = /language-(\w+)/.exec(className || "")
              const isCodeBlock = match || (className && className.includes("language-"))

              if (!isCodeBlock) {
                return (
                  <code
                    className="bg-slate-100 dark:bg-slate-800 text-slate-900 dark:text-slate-100 px-1.5 py-0.5 rounded font-mono text-sm"
                    {...rest}
                  >
                    {children}
                  </code>
                )
              }
              return (
                <code
                  className="block bg-slate-900 text-slate-100 p-3 rounded-md font-mono text-xs overflow-x-auto border border-slate-700"
                  {...rest}
                >
                  {children}
                </code>
              )
            },
            pre({ children }) {
              return <div className="not-prose my-2">{children}</div>
            },
          }}
        >
          {content}
        </ReactMarkdown>
      </article>
    </div>
  )
}
