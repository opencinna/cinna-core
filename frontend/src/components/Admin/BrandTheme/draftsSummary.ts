/** "3 of 20 kept, newest first" — the Drafts card's and Sheet's one line. */
export function draftsSummary(count: number, maxDrafts?: number): string {
  return maxDrafts
    ? `${count} of ${maxDrafts} kept, newest first`
    : `${count} kept, newest first`
}

/** The cap is reached: nothing more can be uploaded until a draft goes. */
export function draftsFull(count: number, maxDrafts?: number): boolean {
  return maxDrafts != null && count >= maxDrafts
}
