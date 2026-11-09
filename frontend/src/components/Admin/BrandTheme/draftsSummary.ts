import type { BrandThemeDraftPublic } from "@/client"
import { isPublished } from "./brandThemeQueries"

/** "3 of 20 kept" — the Brand theme card's and Sheet's count. */
export function draftsSummary(count: number, maxDrafts?: number): string {
  return maxDrafts ? `${count} of ${maxDrafts} kept` : `${count} kept`
}

/** The cap is reached: nothing more can be uploaded until a draft goes. */
export function draftsFull(count: number, maxDrafts?: number): boolean {
  return maxDrafts != null && count >= maxDrafts
}

/**
 * The published theme first, then the rest as the list returns them (newest
 * first). The card caps at five rows, so without the pin a live theme older
 * than five newer uploads would drop off the card that answers "what is
 * live". The card and its Sheet both order through here.
 */
export function orderThemes(
  themes: BrandThemeDraftPublic[],
): BrandThemeDraftPublic[] {
  const live = themes.filter(isPublished)
  return live.length === 0
    ? themes
    : [...live, ...themes.filter((theme) => !isPublished(theme))]
}
