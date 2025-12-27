import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

interface MarkdownRendererProps {
  content: string
  className?: string
}

export function MarkdownRenderer({ content, className = "" }: MarkdownRendererProps) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code(props) {
            const { className, children, ...rest } = props as any
            // Check if it's a code block by looking for language class
            const match = /language-(\w+)/.exec(className || '')
            const isCodeBlock = match || (className && className.includes('language-'))

            if (!isCodeBlock) {
              // Inline code - subtle background highlight that adapts to theme
              return (
                <code className="bg-slate-100 dark:bg-slate-800 text-slate-900 dark:text-slate-100 px-1.5 py-0.5 rounded font-mono text-sm" {...rest}>
                  {children}
                </code>
              )
            }
            // Code blocks - dark CLI-like appearance
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
    </div>
  )
}
