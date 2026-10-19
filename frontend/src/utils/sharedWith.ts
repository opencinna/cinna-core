import { pluralize } from "@/utils/pluralize"

/** "3 people and 1 group" — a part with 0 is left out; "" when both are 0. */
export function peopleAndGroups(people: number, groups: number): string {
  const parts: string[] = []
  if (people > 0) parts.push(pluralize(people, "person", "people"))
  if (groups > 0) parts.push(pluralize(groups, "group", "groups"))
  return parts.join(" and ")
}

/** "Shared with 3 people and 1 group", or "Not shared yet". */
export function sharedWithSentence(people: number, groups: number): string {
  const who = peopleAndGroups(people, groups)
  return who ? `Shared with ${who}` : "Not shared yet"
}

/** "via Engineering" / "via Engineering +1"; "" when reached directly (D12). */
export function viaGroupsLabel(
  groups: ReadonlyArray<{ name: string }> | null | undefined,
): string {
  if (!groups || groups.length === 0) return ""
  const extra = groups.length > 1 ? ` +${groups.length - 1}` : ""
  return `via ${groups[0].name}${extra}`
}
