/**
 * Component references in chat: `cinna://component/<agent>/[<skill>/]<name>`.
 *
 * The grammar mirrors the backend's parser
 * (`backend/app/services/dashboards/component_refs_service.py`) and the kit's
 * `x-references` (`docs/local_agent_kit/protocols/dashboards.schema.json`):
 * the agent segment is `bundle:<bundle_id>`, `id:<manifest uuid>` or a bare
 * slug, and its prefix is always written.
 *
 * Rendering rules (plan §8): a reference **alone on its line**, or a markdown
 * link to one, becomes a card; one inside backticks or a code block stays
 * plain text. Rendering resolves the reference and runs nothing.
 */

export const COMPONENT_REF_PREFIX = "cinna://component/"

const MAX_REF_LENGTH = 512
const AGENT_SEGMENT =
  "(?:bundle:[A-Za-z0-9][A-Za-z0-9.-]{1,253}|id:[0-9a-fA-F-]{36}|[a-z0-9][a-z0-9-]{1,62})"
const SKILL_SEGMENT = "[a-z0-9]+(?:-[a-z0-9]+)*"
const NAME_SEGMENT = "[a-z0-9][a-z0-9-]{0,63}"
const REF_RE = new RegExp(
  `^cinna://component/(${AGENT_SEGMENT})(?:/(${SKILL_SEGMENT}))?/(${NAME_SEGMENT})$`,
)

export interface ParsedComponentRef {
  /** `bundle:…`, `id:…` or a slug, prefix included. */
  agentSegment: string
  skill: string | null
  name: string
}

export function parseComponentRef(text: string): ParsedComponentRef | null {
  if (text.length > MAX_REF_LENGTH) return null
  const match = REF_RE.exec(text)
  if (!match) return null
  return { agentSegment: match[1], skill: match[2] || null, name: match[3] }
}

export function isComponentRef(text: string): boolean {
  return parseComponentRef(text) !== null
}

/** Only `cinna://component/…` survives `urlTransform` besides the safe defaults. */
export function isComponentRefUrl(url: string): boolean {
  return url.startsWith(COMPONENT_REF_PREFIX)
}

/**
 * The reference in its bundle form (`bundle:<bundle_id>`), the one that means
 * the same thing on every machine and for every teammate. A reference already
 * in that form, or one whose agent has no bundle id, is returned unchanged.
 */
export function toBundleRef(ref: string, bundleId: string | null | undefined) {
  const parsed = parseComponentRef(ref)
  if (!parsed || !bundleId || parsed.agentSegment.startsWith("bundle:"))
    return ref
  const skill = parsed.skill ? `${parsed.skill}/` : ""
  return `${COMPONENT_REF_PREFIX}bundle:${bundleId}/${skill}${parsed.name}`
}

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/

/**
 * Put every reference that sits alone on a line into its own paragraph, so the
 * renderer can draw it as a block. `"Here it is:\ncinna://component/…"` is one
 * markdown paragraph; with blank lines around the reference it is two.
 *
 * Fenced code is left alone (a reference there is code), and so is a line
 * indented by four or more spaces (an indented code block or a list
 * continuation). Inline code never matches: the line would start with a
 * backtick.
 */
export function isolateComponentRefLines(markdown: string): string {
  if (!markdown.includes(COMPONENT_REF_PREFIX)) return markdown
  const lines = markdown.split("\n")
  const out: string[] = []
  let fence: string | null = null
  for (const line of lines) {
    const fenceMatch = FENCE_RE.exec(line)
    if (fenceMatch) {
      const marker = fenceMatch[1]
      if (fence === null) fence = marker[0]
      else if (marker[0] === fence) fence = null
      out.push(line)
      continue
    }
    const trimmed = line.trim()
    const indent = line.length - line.trimStart().length
    if (fence === null && indent < 4 && isComponentRef(trimmed)) {
      out.push("", trimmed, "")
      continue
    }
    out.push(line)
  }
  return out.join("\n")
}
