/**
 * Shared rendering rules for the skills catalog (S6–S11).
 *
 * The catalog answers every refusal with `{code, message, paths?}` in the
 * FastAPI `detail` body (`services/skills/exceptions.py`), so the client picks
 * its tone and its next step from the **code** and prints the sentence the
 * server wrote. Nothing here matches on prose: the codes are the contract.
 *
 * These helpers live in `utils/` rather than beside one dialog because the
 * publish dialog, the install dialog and the package route all have to agree on
 * what a revision is called and on how a refusal reads; a second copy of either
 * is how two surfaces start describing the same 409 differently.
 */
import type { SkillPackageRevisionPublic } from "@/client"

export interface SkillCatalogFailure {
  /** The server's stable code, or `null` when the failure carried none. */
  code: string | null
  /** The server's own sentence, or the caller's fallback. */
  message: string
  /**
   * Files the refusal names. Populated only for `skill_contains_secrets`,
   * relative to the skill folder, so the publisher can be told *which* files
   * rather than sent hunting.
   */
  paths: string[]
}

/**
 * Narrow a thrown `ApiError` to the catalog's coded shape.
 *
 * Deliberately total: a network failure, a 500 with an HTML body and a 422 with
 * FastAPI's own list-shaped `detail` all have to come out as something a dialog
 * can render, and `code: null` is the honest answer for each of them.
 */
export function skillCatalogFailure(
  error: unknown,
  fallback: string,
): SkillCatalogFailure {
  const detail = (error as { body?: { detail?: unknown } })?.body?.detail

  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const shape = detail as {
      code?: unknown
      message?: unknown
      paths?: unknown
    }
    return {
      code: typeof shape.code === "string" ? shape.code : null,
      message:
        typeof shape.message === "string" && shape.message
          ? shape.message
          : fallback,
      paths: Array.isArray(shape.paths)
        ? shape.paths.filter((p): p is string => typeof p === "string")
        : [],
    }
  }

  if (typeof detail === "string" && detail) {
    return { code: null, message: detail, paths: [] }
  }

  const message = (error as { message?: string })?.message
  return { code: null, message: message || fallback, paths: [] }
}

/**
 * The sentence that says what to do about a refusal, keyed by code.
 *
 * Separate from the server's `message`, which says what happened: a publisher
 * reading "this agent has no environment" still needs to be told that creating
 * one is the fix. A code with no entry here renders the server sentence alone,
 * which is the right degradation — inventing a next step for a code this build
 * has not heard of is how a user gets sent somewhere wrong.
 */
export const SKILL_FAILURE_NEXT_STEP: Record<string, string> = {
  no_environment:
    "Create an environment for this agent from its Environment tab, then publish.",
  workspace_unavailable:
    "Start the environment once so its workspace is written to disk, then publish.",
  skill_contains_secrets:
    "Move or delete these files, refresh the Addons tab, then share it again.",
  skill_invalid:
    "Fix the skill in the workspace, refresh the Addons tab, then share it again.",
  skill_too_large: "Trim the skill folder, then publish again.",
  package_id_taken: "Choose a different package id under Advanced.",
  package_id_immutable:
    "Leave the package id as it is — every install and every container manifest references it.",
  package_id_invalid: "Use a reverse-DNS id, for example com.example.my-skill.",
  already_installed:
    "That agent already has this skill. Pick another agent, or update it from the agent's Addons tab.",
  name_conflict:
    "Uninstall the other package from that agent first, or pick a different agent.",
  snapshot_missing:
    "This revision's files are no longer on the server. Pick a different revision.",
  no_revision: "This package has no published revision to install yet.",
  // The Add addon dialog installs marketplace plugins through the same alert.
  // An unsupported entry is unselectable in the list, so this 409 is reached
  // only when a re-sync refused the entry while the dialog was open — which is
  // exactly when the reader needs to be told the list has moved under them.
  plugin_unsupported:
    "Pick a different entry. Its marketplace has to publish it in a format this platform can fetch before it can be installed.",
  // Grant refusals from `POST /skills/packages/{id}/grants`, which the share
  // dialog also raises when it sends `grant_emails`.
  user_not_found:
    "No account on this instance has that address. Check the spelling, or ask them to sign up first.",
  self_grant:
    "You are the publisher — you can already see it. Remove yourself from the list.",
  grant_not_found: "That person no longer has access; nothing to remove.",
}

/** "v1.3" when the publisher named a version, "rev 4" when they did not. */
export function skillRevisionLabel(
  rev: Pick<SkillPackageRevisionPublic, "version" | "revision_number">,
): string {
  return rev.version ? `v${rev.version}` : `rev ${rev.revision_number}`
}

/**
 * A package's headline version, for a catalog card's badge.
 *
 * Falls back to the revision number for a package published without a version
 * string, and to `null` for one with no revision at all — which the card must
 * render as *nothing*, never as "v null".
 */
export function skillPackageVersionLabel(pkg: {
  latest_version?: string | null
  latest_revision_number?: number | null
}): string | null {
  if (pkg.latest_version) return `v${pkg.latest_version}`
  if (pkg.latest_revision_number != null)
    return `rev ${pkg.latest_revision_number}`
  return null
}

/** A revision's published size, for the row's detail tooltip. */
export function formatSkillBytes(bytes: number | undefined | null): string {
  if (bytes == null || bytes < 0) return "0 B"
  if (bytes < 1024) return `${bytes} B`
  return `${Math.round(bytes / 1024)} KB`
}

/**
 * The publisher, in one line.
 *
 * Same precedence as the bundle catalog card, so the two catalog sections name
 * a person the same way: display name, then email, then nothing — never a raw
 * user id, which tells the reader less than "unknown publisher" does.
 */
export function skillPublisherLabel(pkg: {
  publisher_name?: string | null
  publisher_email?: string | null
}): string {
  return pkg.publisher_name || pkg.publisher_email || "unknown publisher"
}

/**
 * The three visibilities a skill package can have, with the consequence
 * spelled out — the share dialog sets it and the edit dialog changes it, and a
 * second copy is how the same field acquires two different explanations.
 *
 * Ordered least to most visible, so the segments read as a scale rather than
 * as three unrelated choices. "People" sits in the middle because that is
 * where it belongs on that scale, not because it was added last.
 */
export const SKILL_VISIBILITY_OPTIONS = [
  {
    value: "private",
    label: "Private",
    hint: "Private — only you can see and install it",
  },
  {
    value: "users",
    label: "People",
    hint: "People — only the people you name can see and install it",
  },
  {
    value: "public",
    label: "Public",
    hint: "Public — anyone on this instance can install it",
  },
]
