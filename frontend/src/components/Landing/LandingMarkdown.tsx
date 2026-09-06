import ReactMarkdown, { type Components } from "react-markdown"
import rehypeSanitize from "rehype-sanitize"
import remarkGfm from "remark-gfm"

/**
 * Markdown → React for the **public, anonymous** landing surface.
 *
 * THIS FILE IS A SECURITY BOUNDARY. Read before editing.
 * ------------------------------------------------------
 * `ServerConfig.landing_markdown` is superuser-authored copy that
 * `GET /server-config/landing` serves to *unauthenticated* visitors of
 * `/start`. The author is trusted; the audience is the whole internet. That
 * asymmetry — trusted source, untrusted audience — is why this renderer
 * exists separately from the one chat uses.
 *
 * Why it is a separate file rather than a prop on `Chat/MarkdownRenderer`
 * ----------------------------------------------------------------------
 * The property being protected is *structural*, not behavioural: the plugin
 * list below is reachable only from this file. A `rehype-raw` added to the
 * chat renderer for some unrelated chat feature — an agent-authored message,
 * a tool-call transcript — cannot reach `/start`, because `/start` does not
 * share that list. Passing options into a shared renderer would put both
 * surfaces back on one plugin list and reinstate exactly the failure this is
 * built to rule out: stored XSS served to every anonymous visitor, arriving
 * in a diff that never touches `/start` or any file named for it.
 *
 * The component overrides below are deliberately duplicated from the chat
 * renderer rather than imported from it. They are ~20 lines of Tailwind, and
 * a boundary file should be auditable end to end without following imports.
 * Keeping them local also means no shared module exists through which a
 * `components` override (which could reach for `dangerouslySetInnerHTML`)
 * could arrive here from a change aimed at chat.
 *
 * NEVER add `rehype-raw` — or any plugin that turns HTML source text into
 * elements — to the list below. Raw HTML is inert here twice over, and both
 * reasons are load-bearing:
 *
 *   1. `react-markdown` does not parse HTML source into elements at all
 *      unless `rehype-raw` is present. It is absent, and must stay absent.
 *   2. `rehype-sanitize` runs on every render with the default (GitHub)
 *      schema, which drops the `raw` nodes that carry that HTML source. This
 *      is the active guarantee: it holds even if (1) is ever violated, in
 *      either plugin order — placed before `rehype-raw` it strips the raw
 *      nodes before they can be parsed, placed after it strips the elements
 *      they parsed into.
 *
 * Sanitising costs nothing here: rendered against ordinary admin prose —
 * headings, lists, links, emphasis, blockquotes, GFM tables, task lists,
 * images and fenced code with `language-*` classes — the sanitised output is
 * byte-identical to the unsanitised output. It is deliberately NOT retrofitted
 * onto the chat renderer, whose richer output (syntax-highlight classNames and
 * similar) is a far larger regression surface and a separate decision.
 *
 * The server-side `reject_raw_html` check on `ServerConfigUpdate` is defence
 * in depth and an admin-facing affordance — a write-time predicate that models
 * CommonMark fence semantics, i.e. a parser competing with a parser. It is not
 * the boundary. This render path is.
 */
interface LandingMarkdownProps {
  content: string
  className?: string
}

const components: Components = {
  // `node` is destructured out and dropped on purpose: react-markdown passes
  // it to every component, and spreading it onto a DOM element makes React
  // complain about an unknown attribute.
  code({ node: _node, className, children, ...rest }) {
    const isCodeBlock = /language-/.test(className ?? "")

    if (!isCodeBlock) {
      // Inline code — subtle background highlight that adapts to theme.
      return (
        <code
          className="bg-slate-200 text-slate-800 dark:bg-slate-900 dark:text-slate-100 px-1.5 py-0.5 rounded font-mono text-sm"
          {...rest}
        >
          {children}
        </code>
      )
    }

    // Code blocks — dark CLI-like appearance.
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
}

export function LandingMarkdown({
  content,
  className = "",
}: LandingMarkdownProps) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // Raw HTML must never be enabled on this list. See the file comment.
        rehypePlugins={[rehypeSanitize]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
