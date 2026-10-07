/**
 * Opens a producer agent's harvested OpenAPI spec as rendered docs in a
 * dedicated browser tab (the ``/agent-api-spec/$agentId`` route).
 *
 * A stable per-agent window name lets repeated clicks reuse/focus the same tab
 * instead of piling up duplicates. The route lives inside the same SPA, so it
 * reuses the JWT in localStorage to fetch the spec — no separate auth.
 */
export function openAgentApiSpec(agentId: string): void {
  window.open(`/agent-api-spec/${agentId}`, `cinna-agent-api-spec-${agentId}`)
}

/**
 * Opens the spec for the HOLDER of an issued key, through
 * ``GET /credentials/{id}/agent-api-key/spec`` — the producer spec route above
 * is owner-gated and would 404 for anyone else. Same viewer route, carrying the
 * credential as a search param so the page fetches through the holder path.
 */
export function openAgentApiKeySpec(
  producerAgentId: string,
  credentialId: string,
): void {
  window.open(
    `/agent-api-spec/${producerAgentId}?credential=${encodeURIComponent(credentialId)}`,
    `cinna-agent-api-key-spec-${credentialId}`,
  )
}
