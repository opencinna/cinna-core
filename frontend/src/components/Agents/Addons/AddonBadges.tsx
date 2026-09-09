import { Upload } from "lucide-react"

import type { AddonPublic } from "@/client"
import { RowFlag } from "@/components/Common/ListRow"
import { Badge } from "@/components/ui/badge"

interface AddonBadgesProps {
  addon: AddonPublic
}

/**
 * **Local** — the one origin badge a local skill's row carries.
 *
 * A list that merges four origins has no unmarked default, and a local skill
 * carries neither a marketplace nor an author, so without this the row built in
 * this agent is the one row with nothing on it. It occupies the slot `author`
 * fills on every other source's row — the same question, answered for the one
 * source that has no author to name — which is what keeps the row inside the
 * two-badge exception §2 already grants rather than past it.
 *
 * "Published" is deliberately **not** a second badge here: see
 * {@link AddonPublishedFlag}.
 */
export function AddonLocalBadge({ addon }: AddonBadgesProps) {
  if (addon.source !== "local") return null
  return (
    <Badge variant="outline" className="h-5">
      Local
    </Badge>
  )
}

/**
 * **Published** — "this one of mine is in the catalog", as a flag rather than a
 * badge.
 *
 * It is a passive per-row fact the user reads and cannot click, which is what
 * §2 "Row flags" describes: one glyph, a required tooltip, an `sr-only` label,
 * and no cost to the name's width. A third `Badge` would have put the row past
 * the two the guidelines allow it, on the title line that is contested space
 * either way — and the word itself is not lost, because `AddonDetailDialog`
 * still spells it out beside the package id.
 *
 * Rendered in the flags cluster next to the update flag, so a local row's
 * right-hand side reads the same way every other row's does.
 */
export function AddonPublishedFlag({ addon }: AddonBadgesProps) {
  if (addon.source !== "local" || !addon.published_package_id) return null
  return <RowFlag icon={Upload} label="Published to the skills catalog" />
}
