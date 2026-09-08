/**
 * Shared rendering rules for the agent skill index.
 *
 * The backend gives every flagged condition a stable `code` plus a human
 * `message` (`SkillIssuePublic`), so the client picks its tone from the code
 * and prints the sentence the server wrote — it never matches on prose. These
 * helpers live here rather than in the card because the card, the row and the
 * "Show all" Sheet must agree on the sort order and on what a dot means; a
 * second copy of the ordering is how a preview list stops previewing the rows
 * that need attention.
 */
import type { SkillEntryPublic } from "@/client"
import type { RowStatus } from "@/components/Common/ListRow"

/**
 * Response-level `error` on `AgentSkillsPublic` — why the index could not be
 * read at all. Each code has a different fix, which is the whole reason the
 * server distinguishes them: refresh wakes a sleeping environment, but only a
 * rebuild gives a pre-feature container the endpoint.
 */
const INDEX_ERROR_COPY: Record<string, string> = {
  // Verbatim from the plan's copy list: a container built before agent skills
  // existed has no `/config/skills`, and no amount of refreshing adds one.
  adapter_error: "Rebuild the environment to enable skills.",
  env_not_running:
    "The environment is asleep. Refresh to wake it and re-read the skills.",
  parse_error: "Couldn't read the skills folder. Refresh to try again.",
}

/**
 * The sentence for an index-level error code.
 *
 * The fallback is deliberately neutral rather than the `parse_error` copy: a
 * code this map does not know is by definition one whose fix we cannot name,
 * and telling the user to refresh a folder that was never the problem sends
 * them somewhere wrong.
 */
export function skillsIndexErrorCopy(code: string): string {
  return (
    INDEX_ERROR_COPY[code] ??
    "The skills index couldn't be read. Refresh to try again."
  )
}

/**
 * The row's leading dot.
 *
 * Rendered unconditionally, including for a healthy skill: `ListRow` only
 * draws `status` when it is given one, so an "ok means no dot" rule would
 * indent every clean row differently from every flagged one. The plan's intent
 * — do not shout about a healthy skill — is kept by leaving colour out of the
 * flags and out of the title, not by removing the dot.
 */
export function skillRowStatus(entry: SkillEntryPublic): RowStatus {
  if (entry.error) {
    return {
      tone: "error",
      label: entry.error.message || "This skill is not valid.",
    }
  }
  if (entry.warning) {
    return {
      tone: "warning",
      label: entry.warning.message || "This skill has a warning.",
    }
  }
  return { tone: "on", label: "Projected — the engine can see this skill" }
}

/** A skill's on-disk size, for the row's detail tooltip. */
export function formatSkillSize(bytes: number | undefined): string {
  if (bytes == null || bytes < 0) return "0 B"
  if (bytes < 1024) return `${bytes} B`
  return `${Math.round(bytes / 1024)} KB`
}

/**
 * Attention first, then alphabetical: invalid skills, then warned ones, then
 * the rest by name. The card previews five rows, so the row that needs a
 * decision must be one of them.
 */
export function sortSkills(entries: SkillEntryPublic[]): SkillEntryPublic[] {
  const rank = (entry: SkillEntryPublic) =>
    entry.error ? 0 : entry.warning ? 1 : 2
  return [...entries].sort((a, b) => {
    const byRank = rank(a) - rank(b)
    if (byRank !== 0) return byRank
    return a.name.localeCompare(b.name)
  })
}

/**
 * React key for a skill row.
 *
 * The name alone is not unique: a local skill and a plugin-supplied one can
 * carry the same name (the command popup dedupes them for exactly that
 * reason), and two rows keyed the same would make React reuse one row's dialog
 * state for the other.
 */
export function skillKey(entry: SkillEntryPublic): string {
  return `${entry.source ?? "local"}:${entry.plugin_ref ?? ""}:${entry.name}`
}
