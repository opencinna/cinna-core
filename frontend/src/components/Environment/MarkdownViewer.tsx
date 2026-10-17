import type { MouseEvent } from "react"
import ReactMarkdown, { type UrlTransform } from "react-markdown"
import remarkGfm from "remark-gfm"

import { cn } from "@/lib/utils"

interface MarkdownViewerProps {
  content: string
  className?: string
  /**
   * Rewrites every link and image URL before render (react-markdown's own
   * hook). The artifact viewer points relative paths at its capability URLs;
   * the caller is responsible for ending in `defaultUrlTransform`.
   */
  urlTransform?: UrlTransform
  /**
   * Called for a click on a rendered link, with its href (after
   * `urlTransform`). Return
   * true to say the click was handled in-app (the default navigation is then
   * prevented).
   */
  onLinkClick?: (href: string) => boolean
}

export function MarkdownViewer({
  content,
  className,
  urlTransform,
  onLinkClick,
}: MarkdownViewerProps) {
  return (
    <div className="h-full overflow-auto p-6">
      <article
        className={cn("prose prose-slate dark:prose-invert max-w-none", className)}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          urlTransform={urlTransform}
          components={{
            a({ node: _node, href, children, ...rest }) {
              const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
                if (onLinkClick && href && onLinkClick(href)) {
                  e.preventDefault()
                }
              }
              return (
                <a href={href} onClick={onClick} {...rest}>
                  {children}
                </a>
              )
            },
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
