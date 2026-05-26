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
