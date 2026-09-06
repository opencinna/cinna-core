import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

/**
 * Markdown → React, with no sanitiser configured — and that is the contract,
 * not an oversight.
 *
 * Raw HTML in the source is inert because `react-markdown` does not parse it
 * unless `rehype-raw` is added, and dangerous URL schemes are stripped by its
 * built-in `defaultUrlTransform`. Both are *defaults*. Nothing here re-states
 * them, so nothing here would complain if they changed.
 *
 * NO ANONYMOUS CONSUMER — and that is now structural
 * ---------------------------------------------------
 * This file once had one. `routes/start.tsx` renders
 * `ServerConfig.landing_markdown` to unauthenticated visitors on the public
 * `/start` page: the author is still a superuser, so the trust in the *source*
 * was unchanged, but the *audience* was the whole internet. Adding
 * `rehype-raw` here — for a chat feature, for an agent-authored message, for
 * anything — would have turned admin-authored landing copy into stored XSS
 * served to every anonymous visitor, in a diff that never touched `/start`.
 *
 * That coupling is gone. `/start` now renders through
 * `components/Landing/LandingMarkdown.tsx`, which has its own explicit plugin
 * list and does **not** inherit this file's. It additionally runs
 * `rehype-sanitize`, so raw HTML is inert there by assertion rather than by
 * the absence of a plugin. Changes here cannot reach the public page.
 *
 * The warning below still stands, on chat's own account:
 *
 * Every caller of *this* renderer is behind auth, but "behind auth" is not
 * "trusted content". Chat renders agent output, tool-call transcripts and
 * other people's messages; adding `rehype-raw` here makes any of those able to
 * inject markup into a signed-in user's page. If that plugin is ever genuinely
 * wanted, pair it with `rehype-sanitize` in the same commit — and do not
 * "simplify" `/start` back onto this renderer to match.
 */
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
                <code className="bg-slate-200 text-slate-800 dark:bg-slate-900 dark:text-slate-100 px-1.5 py-0.5 rounded font-mono text-sm" {...rest}>
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
