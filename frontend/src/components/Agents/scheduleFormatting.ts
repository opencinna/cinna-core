/**
 * Date formatting for the Schedules concern.
 *
 * Lives beside its consumers rather than in `src/utils/` because the six
 * files of this concern are its only callers. `EnvironmentActionLogsModal`
 * mirrors `formatExecutedAt` with its own copy; unifying the two is a
 * separate change and is deliberately not made here.
 */

/**
 * Parse a timestamp the API returned.
 *
 * FastAPI serialises naive UTC datetimes without a zone designator, and
 * `new Date("2026-09-08T07:00:00")` is read as *local* time by the browser.
 * Appending the `Z` the server omitted is what keeps "Next" honest.
 */
export function parseServerDate(isoDate: string): Date | null {
  // Test for a zone rather than for a "+": a negative offset ("…T07:00:00-05:00")
  // would otherwise fall into the naive branch and get a `Z` appended, which
  // is not a date at all. Schedule columns serialise naive today, but this
  // module is exported and nothing stops the next caller from passing an
  // aware timestamp.
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(isoDate)
  const normalised = !hasZone && isoDate.includes("T") ? `${isoDate}Z` : isoDate
  const date = new Date(normalised)
  return Number.isNaN(date.getTime()) ? null : date
}

/**
 * Sort key for a schedule's next run. Unparseable timestamps sort last.
 *
 * `MAX_SAFE_INTEGER` rather than `Infinity`: the key is used as a subtraction
 * in a comparator, and `Infinity - Infinity` is `NaN` — an inconsistent
 * comparator the moment two timestamps are both unparseable.
 */
export function nextExecutionSortKey(isoDate: string): number {
  return parseServerDate(isoDate)?.getTime() ?? Number.MAX_SAFE_INTEGER
}

/**
 * The long form: "Monday, September 8, 2026, 07:00 AM GMT+2".
 *
 * Used where confirming *exactly* when this will fire is the point — the
 * create and edit dialogs, after the timing has been generated. Too long for
 * a row, which is why `formatNextExecutionShort` exists.
 */
export function formatNextExecution(isoDate?: string | null): string {
  if (!isoDate) return "Unknown"
  const date = parseServerDate(isoDate)
  if (!date) return isoDate
  return date.toLocaleString(undefined, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  })
}

/**
 * The row form: "Sep 8, 07:00".
 *
 * One of two facts on a compact row's single metadata line, so it has to fit
 * beside the schedule's description without pushing it out of the row.
 */
export function formatNextExecutionShort(isoDate?: string | null): string {
  if (!isoDate) return "Unknown"
  const date = parseServerDate(isoDate)
  if (!date) return isoDate
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

/**
 * Relative for the recent past ("12m ago"), absolute beyond a week.
 *
 * Execution logs are read to answer "did the last run work", so recency
 * matters more than the wall-clock time of a run eight days ago.
 */
export function formatExecutedAt(isoDate: string): string {
  const date = parseServerDate(isoDate)
  if (!date) return isoDate

  const diffMinutes = Math.floor((Date.now() - date.getTime()) / 60000)
  const diffHours = Math.floor(diffMinutes / 60)
  const diffDays = Math.floor(diffHours / 24)

  if (diffMinutes < 1) return "Just now"
  if (diffMinutes < 60) return `${diffMinutes}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 7) return `${diffDays}d ago`

  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}
