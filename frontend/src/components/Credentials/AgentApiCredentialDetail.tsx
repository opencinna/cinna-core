import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"

import type { CredentialWithData } from "@/client"
import { CredentialsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { AgentApiConnectionView } from "@/components/Credentials/AgentApiConnectionView"
import { AgentApiKeyView } from "@/components/Credentials/AgentApiKeyView"
import { CredentialSharing } from "@/components/Credentials/CredentialSharing"
import { Skeleton } from "@/components/ui/skeleton"
import { useAgentApiKeyForCredential } from "@/hooks/useAgentApiKeys"

interface AgentApiCredentialDetailProps {
  credential: CredentialWithData
  /** Route's latched ``?new=1`` marker — the credential was just created. */
  justCreated?: boolean
}

/**
 * Detail surface for an ``agent_api`` credential, which is really two products
 * behind one type:
 *
 * - a **connection** — one platform agent wired to another's REST API. Machine
 *   -only, shareable.
 * - an **issued key** — a value a human copies into a laptop script, a server,
 *   or a cron job. Issued to one person, revealable by them only, never
 *   shareable.
 *
 * The split is decided by the holder endpoint (see
 * ``useAgentApiKeyForCredential``), resolved here once and used for both which
 * view renders and whether the sharing card exists at all.
 */
export function AgentApiCredentialDetail({
  credential,
  justCreated,
}: AgentApiCredentialDetailProps) {
  const { holder, isResolved, isUnresolvable, error, refetch } =
    useAgentApiKeyForCredential(credential)

  // Wait for the answer rather than flashing the wrong view — the two differ in
  // what they reveal, so guessing is not free.
  if (!isResolved) {
    // Shaped like the key view's cards (the commoner of the two answers).
    return (
      <div className="space-y-6">
        <Skeleton className="h-64 w-full rounded-xl" />
        <div className="grid gap-6 lg:grid-cols-2">
          <Skeleton className="h-80 w-full rounded-xl" />
          <Skeleton className="h-80 w-full rounded-xl" />
        </div>
      </div>
    )
  }

  // Something failed that was NOT "this is not a key". Say so instead of
  // defaulting to the connection view, which would look like a key had lost
  // its value and its status.
  if (isUnresolvable) {
    return (
      <QueryErrorAlert
        error={error}
        fallback="Couldn't load this credential"
        onRetry={refetch}
      />
    )
  }

  if (holder) {
    // No sharing card: issued keys are never shareable (plan D9), so
    // rendering it would only offer a control that always fails.
    return (
      <AgentApiKeyView
        credential={credential}
        holder={holder}
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
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
        <CredentialSharing
          credential={credential}
          hint={<ConnectionSharingHint credentialId={credential.id} />}
        />
      </div>
    </div>
  )
}

/**
 * Plan D10 steering: a connection is how an *agent* calls a producer; a person
 * calling it from their own code needs a key from the producer's API access
 * card. Shown only when the viewer owns the producer — the server says so
 * (``producer_owned_by_viewer``); a 404 on the agent is never used to guess.
 * Shares the connection view's query, so it costs no request.
 */
function ConnectionSharingHint({ credentialId }: { credentialId: string }) {
  const { data } = useQuery({
    queryKey: ["agentApiConnection", credentialId],
    queryFn: () => CredentialsService.readAgentApiConnection({ id: credentialId }),
  })

  if (!data?.producer_owned_by_viewer || !data.producer_agent_id) return null

  return (
    <p className="text-sm text-muted-foreground">
      To let a person call {data.producer_agent_name || "this agent"} from their
      own code, give them a key.{" "}
      <Link
        to="/agent/$agentId"
        params={{ agentId: data.producer_agent_id }}
        hash="integrations"
        className="text-primary hover:underline"
      >
        Open API access
      </Link>
    </p>
  )
}
