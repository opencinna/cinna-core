import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useMemo, useState } from "react"

import type { CredentialWithData } from "@/client"
import { AgentApiService, CredentialsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { AgentApiConnectionScopesCard } from "@/components/Credentials/AgentApiConnectionScopesCard"
import { AgentApiConnectionScopesDialog } from "@/components/Credentials/AgentApiConnectionScopesDialog"
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

      {/* Sharing half-width (left), matching the Template-card layout of
          other types; the connection's scopes take the right half. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
        <CredentialSharing
          credential={credential}
          hint={<ConnectionSharingHint credentialId={credential.id} />}
        />
        <ConnectionScopes
          credentialId={credential.id}
          connectionName={credential.name}
        />
      </div>
    </div>
  )
}

/**
 * Plan D10 steering, both directions: sharing this connection (with bundle
 * re-use allowed) is how other people's agents and the bundles they publish
 * call the producer; a person calling it from their own code needs a key from
 * the producer's API access card. Shown only when the viewer owns the producer — the server says so
 * (``producer_owned_by_viewer``); a 404 on the agent is never used to guess.
 * Shares the connection view's query, so it costs no request.
 */
function ConnectionSharingHint({ credentialId }: { credentialId: string }) {
  const { data } = useQuery({
    queryKey: ["agentApiConnection", credentialId],
    queryFn: () =>
      CredentialsService.readAgentApiConnection({ id: credentialId }),
  })

  if (!data?.producer_owned_by_viewer || !data.producer_agent_id) return null

  return (
    <p className="text-sm text-muted-foreground">
      Share this connection, and allow re-use inside bundles, to let other
      people's agents and the bundles they publish call{" "}
      {data.producer_agent_name || "this agent"}. For one person's own code,
      give them a key instead.{" "}
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

/**
 * S2 — the connection's scopes, and the editor when the viewer owns the
 * producer (``can_edit_scopes``). Shares the connection view's query; the
 * catalog is fetched only for an editor, to mark scopes the API dropped.
 */
function ConnectionScopes({
  credentialId,
  connectionName,
}: {
  credentialId: string
  connectionName: string
}) {
  const [editOpen, setEditOpen] = useState(false)
  const connectionQuery = useQuery({
    queryKey: ["agentApiConnection", credentialId],
    queryFn: () =>
      CredentialsService.readAgentApiConnection({ id: credentialId }),
  })
  const connection = connectionQuery.data
  const scopes = connection?.connection_scopes ?? []
  const producerId = connection?.producer_agent_id ?? null
  const editable = !!connection?.can_edit_scopes && !!producerId

  const catalogQuery = useQuery({
    queryKey: ["agentApiScopeCatalog", producerId],
    queryFn: () =>
      AgentApiService.getAgentApiScopeCatalog({
        agentId: producerId as string,
      }),
    enabled: editable,
  })
  const undeclaredCount = useMemo(() => {
    if (!catalogQuery.data) return 0
    const declared = new Set(catalogQuery.data.scopes.map((s) => s.name))
    return scopes.filter((s) => !declared.has(s)).length
  }, [catalogQuery.data, scopes])

  return (
    <>
      <AgentApiConnectionScopesCard
        scopes={scopes}
        description={
          !connection
            ? "The scopes every agent using this connection calls with."
            : editable
              ? "Every agent using this connection calls with these, on top of its owner's own scopes."
              : "Set by the producer's owner; read-only here."
        }
        reach="This includes people it's shared with and bundles that embed it."
        emptyText="No scopes. Callers get only the scopes their own grant gives them."
        onEdit={editable ? () => setEditOpen(true) : undefined}
        undeclaredCount={undeclaredCount}
        isLoading={connectionQuery.isLoading}
        error={connectionQuery.isError ? connectionQuery.error : undefined}
        onRetry={() => connectionQuery.refetch()}
      />
      {editable && producerId && (
        <AgentApiConnectionScopesDialog
          open={editOpen}
          onOpenChange={setEditOpen}
          credentialId={credentialId}
          producerAgentId={producerId}
          connectionName={connectionName}
          scopes={scopes}
        />
      )}
    </>
  )
}
