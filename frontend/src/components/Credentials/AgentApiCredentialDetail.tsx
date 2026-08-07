import { AlertCircle } from "lucide-react"

import type { CredentialWithData } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { AgentApiConnectionView } from "@/components/Credentials/AgentApiConnectionView"
import { AgentApiKeyView } from "@/components/Credentials/AgentApiKeyView"
import { CredentialSharing } from "@/components/Credentials/CredentialSharing"
import PendingItems from "@/components/Pending/PendingItems"
import { useAgentApiKeyForCredential } from "@/hooks/useAgentApiKeys"

interface AgentApiCredentialDetailProps {
  credential: CredentialWithData
  /** Route's latched ``?new=1`` marker — the credential was just created. */
  justCreated?: boolean
}

/**
 * Detail surface for an ``agent_api`` credential, which is really two products
 * behind one type (plan §2):
 *
 * - a **connection** — one platform agent wired to another's REST API. Machine
 *   -only, anonymous by construction, shareable.
 * - an **external key** — a value a human copies into a laptop script, a
 *   server, or a cron job. Identity-bound, revealable, and never shareable
 *   (plan D4: sharing an identity-bound key means "here, act as user X").
 *
 * The split is decided by the bound token's ``kind``, resolved here once and
 * used for both which view renders and whether the sharing card exists at all.
 * Neither view is a superset of the other — a key has an identity a connection
 * cannot have, and a connection has consumer agents a key never does.
 */
export function AgentApiCredentialDetail({
  credential,
  justCreated,
}: AgentApiCredentialDetailProps) {
  const { key, producerAgentId, isResolved, isUnresolvable } =
    useAgentApiKeyForCredential(credential)

  // Wait for the answer rather than flashing the wrong view — the two differ in
  // what they reveal, so guessing is not free.
  if (!isResolved) {
    return <PendingItems />
  }

  // Something failed that was NOT "you cannot see this producer's keys". Say so
  // instead of defaulting to the connection view, which would look like a key
  // had lost its identity, scopes and value.
  if (isUnresolvable) {
    return (
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertTitle>Could not load this credential</AlertTitle>
        <AlertDescription>
          We could not determine whether this is an external key or an
          agent-to-agent connection, so we are not showing either view. Reload
          the page to try again.
        </AlertDescription>
      </Alert>
    )
  }

  if (key && producerAgentId) {
    // No sharing card: allow_sharing is forced off server-side for keys, so
    // rendering it would only offer a control that always fails.
    return (
      <AgentApiKeyView
        credential={credential}
        apiKey={key}
        producerAgentId={producerAgentId}
        justCreated={justCreated}
      />
    )
  }

  return (
    <div className="space-y-6">
      <AgentApiConnectionView credential={credential} />

      {/* Sharing stays half-width (left), matching the Template-card layout of
          other types. The right half is intentionally empty — agent_api
          connections have no Template-sharing card. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <CredentialSharing credential={credential} />
      </div>
    </div>
  )
}
