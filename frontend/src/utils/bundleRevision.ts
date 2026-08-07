/**
 * Shared rendering helpers for bundle revision identity.
 *
 * A revision is identified by two things: an always-present monotonic
 * ``revision_number`` and an optional publisher-authored ``version`` label.
 * The UI prefers the human label when the publisher set one and falls back to
 * the raw number otherwise, so every surface that names a revision
 * (UpdateAvailableBanner, BundleInstallationCard, ...) must agree on the
 * formatting. Extracted here rather than duplicated per component.
 */

/**
 * Render a revision as ``v<version>`` when a version label exists, else
 * ``rev <number>``. Returns null when neither is known.
 */
export function revisionLabel(
  version: string | null | undefined,
  number: number | null | undefined,
): string | null {
  if (version) return `v${version}`
  if (number != null) return `rev ${number}`
  return null
}
