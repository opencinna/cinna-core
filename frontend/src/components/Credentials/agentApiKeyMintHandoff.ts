/**
 * One-shot handoff of a freshly minted agent-api **external key** value from
 * the dialog that minted it to the detail page it navigates to.
 *
 * Why this exists (plan D4): the key's value is returned exactly twice — by
 * ``POST /agents/{id}/agent-api/keys`` at mint, and by the dedicated, audited
 * ``POST /credentials/{id}/agent-api-key/reveal``. It is deliberately NOT part
 * of ``GET /credentials/{id}/with-data`` any more, so the mint flow can no
 * longer rely on the detail page's own fetch to carry it. Routing the
 * post-mint reveal through the reveal endpoint instead would log a
 * ``AGENT_API_EXTERNAL_KEY_REVEALED`` event for a value the user was just
 * handed — noise in the very audit trail the endpoint exists to keep honest.
 *
 * Deliberately in-memory and single-read: the value survives exactly one
 * client-side navigation, never a reload, and is dropped as soon as it is
 * claimed. Nothing here is persisted.
 *
 * ONE slot, not a map keyed by credential. A mint is always followed
 * immediately by a navigation to that credential, so at most one handoff is
 * ever in flight — and a single slot means an unclaimed value (the user
 * navigated elsewhere before the detail page resolved which view to render)
 * is evicted by the next mint instead of sitting in memory for the lifetime of
 * the tab.
 */

let pending: { credentialId: string; token: string } | null = null

/** Park a just-minted value for the detail page of ``credentialId``. */
export function stashMintedAgentApiKey(
  credentialId: string,
  token: string,
): void {
  pending = { credentialId, token }
}

/**
 * Claim (and drop) a value parked by a mint. Returns ``null`` when there is
 * none for this credential — the normal case for every page open that is not
 * the one step after minting.
 */
export function takeMintedAgentApiKey(credentialId: string): string | null {
  if (pending?.credentialId !== credentialId) return null
  const { token } = pending
  pending = null
  return token
}
