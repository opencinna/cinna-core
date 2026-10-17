/**
 * The code viewer's highlighter, loaded on first use (see CodeFileViewer).
 *
 * lowlight runs highlight.js grammars and returns a hast tree whose text nodes
 * are the file's characters, unescaped and untouched; hast-util-to-jsx-runtime
 * turns that tree into React elements. The file is never parsed as HTML and
 * nothing is set as innerHTML, so `<script>` in a `.py` file is just text.
 */
import type { Element, ElementContent, Root } from "hast"
import { toJsxRuntime } from "hast-util-to-jsx-runtime"
import bash from "highlight.js/lib/languages/bash"
import c from "highlight.js/lib/languages/c"
import cpp from "highlight.js/lib/languages/cpp"
import css from "highlight.js/lib/languages/css"
import go from "highlight.js/lib/languages/go"
import ini from "highlight.js/lib/languages/ini"
import java from "highlight.js/lib/languages/java"
import javascript from "highlight.js/lib/languages/javascript"
import python from "highlight.js/lib/languages/python"
import ruby from "highlight.js/lib/languages/ruby"
import rust from "highlight.js/lib/languages/rust"
import scss from "highlight.js/lib/languages/scss"
import sql from "highlight.js/lib/languages/sql"
import typescript from "highlight.js/lib/languages/typescript"
import xml from "highlight.js/lib/languages/xml"
import yaml from "highlight.js/lib/languages/yaml"
import { createLowlight } from "lowlight"
import type { ReactNode } from "react"
import { Fragment, jsx, jsxs } from "react/jsx-runtime"

const lowlight = createLowlight({
  bash,
  c,
  cpp,
  css,
  go,
  ini,
  java,
  javascript,
  python,
  ruby,
  rust,
  scss,
  sql,
  typescript,
  xml,
  yaml,
})

/**
 * highlight.js scopes → theme tokens, so a skin or dark mode recolours code
 * with the rest of the app. Unlisted scopes render in the text colour.
 */
const SCOPE_CLASSES: Record<string, string> = {
  comment: "text-muted-foreground italic",
  quote: "text-muted-foreground italic",
  meta: "text-muted-foreground",
  keyword: "text-primary",
  "selector-tag": "text-primary",
  doctag: "text-primary",
  string: "text-chart-1 dark:text-chart-3",
  regexp: "text-chart-1 dark:text-chart-3",
  symbol: "text-chart-1 dark:text-chart-3",
  char: "text-chart-1 dark:text-chart-3",
  addition: "text-success",
  number: "text-chart-3 dark:text-chart-4",
  literal: "text-chart-3 dark:text-chart-4",
  built_in: "text-chart-3 dark:text-chart-4",
  type: "text-chart-3 dark:text-chart-4",
  attr: "text-chart-3 dark:text-chart-4",
  attribute: "text-chart-3 dark:text-chart-4",
  property: "text-chart-3 dark:text-chart-4",
  "template-variable": "text-chart-3 dark:text-chart-4",
  title: "text-destructive",
  section: "text-destructive",
  name: "text-destructive",
  "selector-id": "text-destructive",
  "selector-class": "text-destructive",
  deletion: "text-destructive",
  emphasis: "italic",
  strong: "font-semibold",
}

/** `hljs-title function_` → the classes of its first known scope, or none. */
function themeClass(classNames: unknown): string[] | undefined {
  if (!Array.isArray(classNames)) return undefined
  for (const name of classNames) {
    if (typeof name !== "string" || !name.startsWith("hljs-")) continue
    const classes = SCOPE_CLASSES[name.slice(5)]
    if (classes) return classes.split(" ")
  }
  return undefined
}

function retheme(node: ElementContent): ElementContent {
  if (node.type !== "element") return node
  const element: Element = {
    type: "element",
    tagName: "span",
    properties: { className: themeClass(node.properties.className) },
    children: node.children.map(retheme),
  }
  return element
}

/** The source as coloured `span`s; its text content is exactly `source`. */
export function highlightCode(source: string, language: string): ReactNode {
  const tree = lowlight.highlight(language, source)
  const themed: Root = {
    type: "root",
    children: tree.children.map((child) =>
      child.type === "element" || child.type === "text"
        ? retheme(child)
        : child,
    ),
  }
  return toJsxRuntime(themed, { Fragment, jsx, jsxs })
}
