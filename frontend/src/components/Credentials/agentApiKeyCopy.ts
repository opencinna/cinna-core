/**
 * User-visible copy for the agent-api **external key** product, in one place.
 *
 * The product name is still a working name (plan "Open Decisions": "Agent API
 * Key" reads better than "Agent API Credential" next to the other picker
 * entries, and "key" signals copy-me-somewhere). Every surface that names it —
 * the global New-credential picker, the producer card's section header, the
 * credential detail card, toasts and confirm dialogs — reads it from here, so
 * renaming the product is a one-line change.
 *
 * A *key* is the outward-facing sibling of a *connection*: a connection wires
 * one platform agent to another and is machine-only, while a key is copied by a
 * human into a laptop script, a server, or a cron job.
 */

/** Product name, singular. */
export const AGENT_API_KEY_LABEL = "Agent API Key"

/** Product name, plural — section headers and list captions. */
export const AGENT_API_KEY_LABEL_PLURAL = "Agent API Keys"

/**
 * One-line "what is this for". The New-credential picker no longer shows it —
 * the key sits inside the "API & Access" group as an ordinary pill, since from
 * the outside it is just an API key for external use — so there it only seeds
 * the entry's search keywords ("code", "REST" find it). Kept as the canonical
 * phrasing for any surface that does want a one-liner.
 */
export const AGENT_API_KEY_TAGLINE =
  "call an agent's REST API from your own code"

/**
 * The curl a holder runs. ``baseUrl`` must be the PUBLIC base URL the backend
 * returned (``AgentApiTokenService.build_base_url``) — never a
 * container-rewritten one, which is unreachable from outside Docker.
 *
 * A ``null`` ``token`` yields a ``<your-key>`` placeholder. That output is for
 * DISPLAY ONLY — it is what the masked `<pre>` shows. It must never reach the
 * clipboard: copying is expected to produce a runnable command, and a snippet
 * that looks right but 401s is worse than no snippet.
 *
 * So the Copy button passes ``null`` as its `value` while the key is hidden and
 * resolves the real token first (see ``KeyUsageSection``). Do not "simplify"
 * that call site into passing this function's result unconditionally — because
 * it always returns a string, that would short-circuit ``value ?? resolve()``
 * and ship the placeholder.
 */
export function buildAgentApiKeyCurl(
  baseUrl: string,
  token: string | null,
): string {
  const base = (
    baseUrl || "https://your-cinna-host/api/v1/agent-api/<agent-id>"
  ).replace(/\/+$/, "")
  return [
    `curl -H "Authorization: Bearer ${token ?? "<your-key>"}" \\`,
    `  ${base}/<your-endpoint>`,
  ].join("\n")
}

/**
 * Plan D7 — the accurate, deliberately narrow warning for the scope card.
 *
 * Scopes live on the ``(producer, subject)`` grant, not on the key, so multiple
 * keys issued to the same user on the same producer share one scope set. The
 * note must say exactly that and no more: it is NOT an application-wide grant.
 */
export function agentApiKeyScopeNote(
  subjectLabel: string,
  producerLabel: string,
): string {
  return (
    `Scopes apply to ${subjectLabel} on ${producerLabel} — everywhere they ` +
    `call this API, including from their own agents and any other key issued ` +
    `to them. They are not specific to this key, and are managed together ` +
    `with the producer's Access & Scopes card.`
  )
}
