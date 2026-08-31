/**
 * Shared vocabulary for Agent Improvement Requests.
 *
 * The backend keeps `status` as a plain VARCHAR validated against a tuple
 * (`IMPROVEMENT_STATUSES`) rather than a Postgres enum, so the generated client
 * types it as `string`. These helpers are the single place the UI turns that
 * string into a label and a colour, and they degrade gracefully on a value the
 * frontend has not been taught yet.
 */

export const IMPROVEMENT_STATUSES = [
  "new",
  "in_progress",
  "completed",
  "declined",
] as const

export type ImprovementStatus = (typeof IMPROVEMENT_STATUSES)[number]

interface StatusMeta {
  label: string
  /** Badge classes. Full class names only — Tailwind JIT cannot see fragments. */
  badgeClass: string
}

/**
 * Status colours per the plan: `new` violet, `in_progress` blue, `completed`
 * green, `declined` muted. Every badge also carries its text label, so colour
 * is never the only carrier of meaning.
 */
const STATUS_META: Record<ImprovementStatus, StatusMeta> = {
  new: {
    label: "New",
    badgeClass:
      "bg-violet-100 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300",
  },
  in_progress: {
    label: "In progress",
    badgeClass:
      "bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300",
  },
  completed: {
    label: "Completed",
    badgeClass:
      "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
  },
  declined: {
    label: "Declined",
    badgeClass: "bg-muted text-muted-foreground",
  },
}

const UNKNOWN_STATUS: StatusMeta = {
  label: "Unknown",
  badgeClass: "bg-muted text-muted-foreground",
}

export const getImprovementStatusMeta = (status: string): StatusMeta =>
  STATUS_META[status as ImprovementStatus] ?? {
    ...UNKNOWN_STATUS,
    label: status || UNKNOWN_STATUS.label,
  }

export const getImprovementStatusLabel = (status: string): string =>
  getImprovementStatusMeta(status).label

/**
 * Display form of a request id: its first 8 characters, matching the archive
 * filename (`improvement-<short-id>.zip`) and the CLI's extraction directory.
 * The copy affordance next to it always copies the *full* UUID, because the
 * `/account/improvement-requests/{request_id}` routes parse a whole UUID.
 */
export const improvementShortId = (id: string): string => id.slice(0, 8)
