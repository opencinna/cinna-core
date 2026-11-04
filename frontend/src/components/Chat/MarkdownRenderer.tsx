import type { Element, ElementContent, Root } from "hast"
import { useContext, useMemo } from "react"
import ReactMarkdown, {
  type Components,
  defaultUrlTransform,
} from "react-markdown"
import remarkGfm from "remark-gfm"

import {
  isComponentRef,
  isComponentRefUrl,
  isolateComponentRefLines,
} from "@/utils/componentRefs"
import { ComponentReferenceCard } from "./ComponentReferenceCard"
import { ComponentRefChatContext } from "./componentRefContext"

/** Cards per message; further references stay plain text (the resolve route takes ≤ 20). */
const MAX_CARDS = 20

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
 *
 * COMPONENT REFERENCES
 * --------------------
 * `urlTransform` lets exactly one extra scheme through: `cinna://component/…`
 * (`utils/componentRefs`). Everything else still goes through
 * `defaultUrlTransform`, so `javascript:` and friends stay stripped. A
 * paragraph that is only a reference — bare on its own line, or a markdown
 * link to one — renders as a `ComponentReferenceCard`; a reference inside
 * running text keeps its words and is not a link (the scheme does not
 * navigate). Inside backticks or a code block it is plain code.
 *
 * Cards are drawn only inside a chat that provides `ComponentRefChatContext`
 * (not on guest pages or other markdown), never in text that is still
 * streaming (`refCards={false}`: a half-written name is a valid prefix), and
 * at most 20 per message.
 */
function urlTransform(url: string): string {
  return isComponentRefUrl(url) ? url : defaultUrlTransform(url)
}

function isBlank(child: ElementContent): boolean {
  return child.type === "text" && !child.value.trim()
}

/** The reference a paragraph consists of, if that is all it holds. */
function paragraphRef(node: Element | undefined): string | null {
  const children = (node?.children ?? []).filter((c) => !isBlank(c))
  if (children.length !== 1) return null
  const only = children[0]
  if (only.type === "text") {
    const text = only.value.trim()
    return isComponentRef(text) ? text : null
  }
  if (only.type === "element" && only.tagName === "a") {
    const href = only.properties?.href
    return typeof href === "string" && isComponentRef(href) ? href : null
  }
  return null
}

interface MarkdownRendererProps {
  content: string
  className?: string
  /** `false` for text that is still streaming: references stay plain text. */
  refCards?: boolean
}

const REF_PROPERTY = "dataComponentRef"

/**
 * Marks the first `MAX_CARDS` paragraphs that are only a reference, in
 * document order, with the reference to draw. Choosing them from the tree
 * (not by counting during render) keeps the choice stable across re-renders.
 */
function rehypeComponentRefs() {
  return (tree: Root) => {
    let cards = 0
    const walk = (node: Root | Element) => {
      for (const child of node.children) {
        if (child.type !== "element") continue
        if (child.tagName === "p" && cards < MAX_CARDS) {
          const ref = paragraphRef(child)
          if (ref) {
            child.properties = { ...child.properties, [REF_PROPERTY]: ref }
            cards += 1
            continue
          }
        }
        walk(child)
      }
    }
    walk(tree)
  }
}

const CARD_PLUGINS = [rehypeComponentRefs]
const NO_PLUGINS: never[] = []

export function MarkdownRenderer({ content, className = "", refCards = true }: MarkdownRendererProps) {
  const chat = useContext(ComponentRefChatContext)
  const cardsOn = refCards && chat !== null
  // One stable set of overrides per mode: inline functions would be new
  // component types on every render and remount the cards (closing an open
  // Preview or Add dialog).
  const components = useMemo<Components>(
    () => ({
      p({ node, children }) {
        const ref = node?.properties?.[REF_PROPERTY]
        if (cardsOn && typeof ref === "string")
          return <ComponentReferenceCard reference={ref} />
        return <p>{children}</p>
      },
      a({ node: _node, href, children, ...rest }) {
        if (href && isComponentRefUrl(href)) return <span>{children}</span>
        return (
          <a href={href} {...rest}>
            {children}
          </a>
        )
      },
      code(props) {
        const { className, children, ...rest } = props as any
        // Check if it's a code block by looking for language class
        const match = /language-(\w+)/.exec(className || '')
        const isCodeBlock = match || className?.includes('language-')

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
    }),
    [cardsOn],
  )
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={cardsOn ? CARD_PLUGINS : NO_PLUGINS}
        urlTransform={urlTransform}
        components={components}
      >
        {cardsOn ? isolateComponentRefLines(content) : content}
      </ReactMarkdown>
    </div>
  )
}
