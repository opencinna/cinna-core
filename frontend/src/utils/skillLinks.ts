/**
 * Skill links — the shareable address of a catalog skill package.
 *
 * Two forms, a cross-repo contract with cinna-desktop (which parses both and
 * reduces either to `{ origin, packageId }`):
 *
 *   https://<origin>/get/skill/<package_id>      what people copy and share
 *   cinna://skill/<package_id>?server=<origin>   the public page's button
 *
 * `<package_id>` is the reverse-DNS id, never the package uuid, so a link
 * survives anything but a delete and always means "the latest version". The
 * link grants nothing: whoever opens it still needs the package's own access.
 * Change the grammar here and in the desktop's `shared/skillLink.ts` together.
 */
import { resolveServerOrigin } from "@/utils"

/**
 * The ids a skill link carries. A superset of what publish mints (which also
 * forbids `_` and a leading `.`/`-`), so a link never refuses an id the
 * catalog could hold; the backend's lookup route applies the same pattern.
 */
export const SKILL_LINK_PACKAGE_ID_RE = /^[A-Za-z0-9._-]{1,255}$/

export function isValidSkillLinkPackageId(packageId: string): boolean {
  return SKILL_LINK_PACKAGE_ID_RE.test(packageId)
}

/**
 * The https link **Copy install link** copies: this page's origin, because it
 * is the web app's own `/get/skill/…` route the recipient's browser opens.
 */
export function skillInstallLink(packageId: string): string {
  return `${window.location.origin}/get/skill/${packageId}`
}

/**
 * The deep link the public page hands Cinna Desktop.
 *
 * `server` is the backend origin (`resolveServerOrigin`), the same value
 * `cinna://connect` carries, because the desktop discovers and resolves the
 * catalog against it; on a same-host deployment it is this page's origin. It
 * is written as-is, not percent-encoded: an origin holds no `&`, `#` or `?`,
 * and the agreed grammar is `cinna://skill/<package_id>?server=<origin>`
 * verbatim. The id is safe by `SKILL_LINK_PACKAGE_ID_RE`.
 */
export function cinnaSkillDeepLink(packageId: string): string {
  return `cinna://skill/${packageId}?server=${resolveServerOrigin()}`
}
