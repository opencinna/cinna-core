/**
 * Validator findings grouped the way an author fixes them: what concerns the
 * whole package, then each mode. Pure and import-free, so the grouping is
 * unit-tested on its own (`tests/brandThemeAdmin.test.mjs`).
 */

export type IssueGroupKey = "general" | "dark" | "light"

/** The fixed order, and the order the preview's panes follow (Dark, Light). */
const GROUPS: ReadonlyArray<readonly [IssueGroupKey, string]> = [
  ["general", "General"],
  ["dark", "Dark"],
  ["light", "Light"],
]

const MODE_PATH = /^(?:modes|logo|background)\.(dark|light)(?:\.|$)/

/**
 * `modes.dark.*`, `logo.dark`, `background.dark` → dark (and the same for
 * light); everything else (`schema`, `name`, `copy.*`, `package`, a bare
 * `background`, …) → general.
 */
export function issueGroup(path: string): IssueGroupKey {
  const match = MODE_PATH.exec(path)
  return match ? (match[1] as IssueGroupKey) : "general"
}

export interface IssueGroup<T> {
  key: IssueGroupKey
  label: string
  issues: T[]
}

/** Non-empty groups only, in the order General · Dark · Light. */
export function groupIssues<T extends { path: string }>(
  issues: readonly T[],
): IssueGroup<T>[] {
  return GROUPS.map(([key, label]) => ({
    key,
    label,
    issues: issues.filter((issue) => issueGroup(issue.path) === key),
  })).filter((group) => group.issues.length > 0)
}
