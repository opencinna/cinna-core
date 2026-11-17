import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useMemo, useState } from "react"

import type { AgentPublic, ApiError } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
import type { AgentOption } from "@/components/Common/AgentSelectorDialog"
import type { UserSelectValue } from "@/components/Common/UserSelectField"
import {
  type AgentApiAccessMode,
  AgentApiAccessModeInfo,
} from "@/components/Credentials/AgentApiAccessModeInfo"
import {
  AgentApiConnectionFields,
  defaultAgentApiConnectionName,
} from "@/components/Credentials/AgentApiConnectionFields"
import { expiryDays } from "@/components/Credentials/AgentApiKeyAdvancedFields"
import { AgentApiKeyFields } from "@/components/Credentials/AgentApiKeyFields"
import {
  AgentApiKeyIssuedPanel,
  type IssuedToOther,
} from "@/components/Credentials/AgentApiKeyIssuedPanel"
import { AgentApiProducerField } from "@/components/Credentials/AgentApiProducerField"
import { unknownScopeMessage } from "@/components/Credentials/agentApiConnectionScopes"
import { AGENT_API_KEY_LABEL } from "@/components/Credentials/agentApiKeyCopy"
import { stashMintedAgentApiKey } from "@/components/Credentials/agentApiKeyMintHandoff"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface AgentApiKeyDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * Fixed producer, from the producer's own API access card. Omitted on
   * `/credentials` › New, which shows the producer picker instead.
   */
  producerAgentId?: string
  /** Prefilled recipient ("Give another key" on a person). Defaults to me. */
  initialRecipient?: UserSelectValue | null
  /**
   * With no `initialRecipient`, start on me (default) or leave the recipient
   * empty so the user must pick someone — "Issued Credentials" uses false.
   */
  defaultRecipientToMe?: boolean
  /**
   * What the dialog opens on; the user can switch inside it. Defaults to a
   * key. Re-applied on every open.
   */
  initialMode?: AgentApiAccessMode
}

/**
 * "Give API access" — issue a key on a producer to myself or to a colleague,
 * or (connection mode) create a shareable connection to it. Behind the
 * producer's API access card ("Give access", "Give another key", "Create
 * shareable connection") and `/credentials` › New › {AGENT_API_KEY_LABEL} /
 * "Agent API connection"; the same form in both, the second only adds the
 * producer picker. An info panel at the top says which of the two the user
 * needs and switches between them.
 *
 * The key lands in the **recipient's** credentials. Issued to me, the flow
 * ends where the value is (the detail page, value shown once from the issue
 * response). Issued to someone else, the issuer never sees the value
 * (``token`` is ``null``), so the dialog ends on a success panel instead.
 *
 * A key's scopes are a convenience that upserts the ``(producer, recipient)``
 * grant; they live there, never on the key. A connection's scopes live on the
 * connection itself and reach everyone who calls through it. Expiry, name and slot have defaults and
 * wait behind one Advanced disclosure. The slot is sent ``null`` when left
 * empty so the server's default (plan D23) stays the one source of the slug;
 * the placeholder only shows it.
 */
export function AgentApiKeyDialog({
  open,
  onOpenChange,
  producerAgentId,
  initialRecipient,
  defaultRecipientToMe = true,
  initialMode = "key",
}: AgentApiKeyDialogProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { user: currentUser } = useAuth()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const me = useMemo<UserSelectValue | null>(
    () =>
      currentUser
        ? {
            userId: currentUser.id,
            label: currentUser.full_name || currentUser.email,
          }
        : null,
    [currentUser],
  )

  const [mode, setMode] = useState<AgentApiAccessMode>(initialMode)
  const [selectedProducerId, setSelectedProducerId] = useState("")
  const [recipient, setRecipient] = useState<UserSelectValue | null>(null)
  const [label, setLabel] = useState("")
  const [slot, setSlot] = useState("")
  const [scopes, setScopes] = useState<string[]>([])
  const [expiry, setExpiry] = useState("never")
  const [issued, setIssued] = useState<IssuedToOther | null>(null)
  // A connection's `unknown_scope` refusal, shown under its scope editor.
  const [scopesError, setScopesError] = useState<string | null>(null)

  // Reset on every open so a second visit never inherits the first one's
  // recipient or scopes. Keyed on `open` so a close→open transition re-fires.
  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) {
      setMode(initialMode)
      setSelectedProducerId("")
      setRecipient(initialRecipient ?? (defaultRecipientToMe ? me : null))
      setLabel("")
      setSlot("")
      setScopes([])
      setExpiry("never")
      setIssued(null)
      setScopesError(null)
    }
  }

  const isProducerFixed = !!producerAgentId
  const producerId = producerAgentId ?? selectedProducerId

  // Scope names are per-producer (that agent's policy.yaml), so switching
  // producers must not carry a scope over to an API where it means nothing.
  const [lastProducerId, setLastProducerId] = useState(producerId)
  if (producerId !== lastProducerId) {
    setLastProducerId(producerId)
    setScopes([])
    setScopesError(null)
  }

  const handleScopesChange = (next: string[]) => {
    setScopes(next)
    setScopesError(null)
  }

  const agentsQuery = useQuery({
    queryKey: ["agents", "agent-api-key-producers"],
    queryFn: () => AgentsService.readAgents({ limit: 200 }),
    enabled: open && !isProducerFixed,
  })
  const { data: fixedProducer } = useQuery({
    queryKey: ["agent", producerAgentId],
    queryFn: () => AgentsService.readAgent({ id: producerAgentId as string }),
    enabled: open && isProducerFixed,
  })

  // Only agents that expose a REST API AND are owned by the caller can be
  // producers — issuing is owner-gated, so anything else would only 404.
  const producers = useMemo<AgentPublic[]>(
    () =>
      (agentsQuery.data?.data ?? []).filter(
        (a) => a.agent_api_enabled && a.owner_id === currentUser?.id,
      ),
    [agentsQuery.data, currentUser?.id],
  )
  const producerOptions: AgentOption[] = producers.map((a) => ({
    id: a.id,
    name: a.name,
    colorPreset: a.ui_color_preset,
  }))
  const producer: AgentPublic | undefined = isProducerFixed
    ? fixedProducer
    : producers.find((a) => a.id === producerId)
  const producerName = producer?.name || ""

  const catalogQuery = useQuery({
    queryKey: ["agentApiScopeCatalog", producerId],
    queryFn: () =>
      AgentApiService.getAgentApiScopeCatalog({ agentId: producerId }),
    enabled: open && !!producerId,
  })
  const catalogData = catalogQuery.data
  const catalogLoaded = catalogQuery.isSuccess
  // A connection takes catalog names only (the server is strict). Scopes typed
  // in key mode carry over on a mode switch, so they are marked here and
  // block Create until removed.
  const declaredScopes = useMemo(
    () =>
      catalogData ? new Set(catalogData.scopes.map((s) => s.name)) : undefined,
    [catalogData],
  )
  const hasUndeclaredScopes =
    !!declaredScopes && scopes.some((s) => !declaredScopes.has(s))

  const recipientIsMe = !!recipient && recipient.userId === currentUser?.id
  const recipientName = recipient?.label || "They"

  const createMutation = useMutation({
    mutationFn: () =>
      AgentApiService.createAgentApiKey({
        agentId: producerId,
        requestBody: {
          label: label.trim() || null,
          issued_to_user_id: recipient?.userId ?? null,
          service_uri: slot.trim() || null,
          // `null` leaves an existing grant untouched — only send a scope set
          // when one was chosen, so a second key never clears their scopes.
          scopes: scopes.length > 0 ? scopes : null,
          expires_in_days: expiryDays(expiry),
        },
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({
        queryKey: agentApiAccessQueryKey(producerId),
      })

      if (!recipientIsMe) {
        setIssued({
          recipientName,
          keyLabel: created.label || `${producerName || "Agent"} API key`,
          producerName: producerName || "the agent",
        })
        return
      }

      showSuccessToast(`${AGENT_API_KEY_LABEL} created`)
      onOpenChange(false)
      if (created.credential_id) {
        // The issue response is the only place this value comes from for
        // free; going through reveal would audit a disclosure nobody asked
        // for. Hand it to the detail page directly.
        if (created.token) {
          stashMintedAgentApiKey(created.credential_id, created.token)
        }
        navigate({
          to: "/credential/$credentialId",
          params: { credentialId: created.credential_id },
          search: { new: 1 },
        })
      }
    },
    onError: handleError.bind(showErrorToast),
  })

  // A connection has no consumer here: it lands in the producer's workspace
  // and is shared from its own page, which is where this flow ends.
  const connectMutation = useMutation({
    mutationFn: () =>
      AgentApiService.connectAgentApi({
        agentId: producerId,
        requestBody: {
          credential_label:
            label.trim() || defaultAgentApiConnectionName(producerName),
          service_uri: slot.trim() || null,
          // Always sent: `[]` is a connection with no scopes of its own.
          scopes,
        },
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({
        queryKey: ["agentApiConnections", producerId],
      })
      showSuccessToast(
        "Connection created — turn on sharing here to let others use it",
      )
      onOpenChange(false)
      navigate({
        to: "/credential/$credentialId",
        params: { credentialId: created.credential_id },
      })
    },
    onError: (err) => {
      const message = unknownScopeMessage(err)
      if (message) setScopesError(message)
      else handleError.call(showErrorToast, err as ApiError)
    },
  })

  const isPending = createMutation.isPending || connectMutation.isPending
  const isConnection = mode === "connection"

  // External access gates keys only; a connection is not affected by it. The
  // card's "Give access" is disabled while it is off, but switching from
  // connection mode reaches key mode anyway, so the gate is re-derived here
  // for the fixed producer too.
  const externalAccessOff =
    !isConnection && !!producer && !producer.agent_api_external_access_enabled
  const canSubmit =
    !!producerId &&
    // `!!producer`: a fixed producer loads after open, and its External
    // access must be known before a key is offered.
    !!producer &&
    (isConnection || !!recipient) &&
    !(isConnection && hasUndeclaredScopes) &&
    // Scopes can't be checked until the catalog loads; none selected needs no check.
    !(isConnection && scopes.length > 0 && !catalogLoaded) &&
    !externalAccessOff &&
    !isPending

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape / outside-click do nothing while the key is being issued.
        if (isPending) return
        onOpenChange(next)
      }}
    >
      <DialogContent
        className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-lg [&>*]:min-w-0"
        // The first focusable is the info panel's mode switch; focused on
        // open, an Enter would reshape the form. The body takes focus instead.
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement).focus()
        }}
      >
        {issued ? (
          <AgentApiKeyIssuedPanel
            {...issued}
            onDone={() => onOpenChange(false)}
          />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>
                {isConnection
                  ? "Create shareable connection"
                  : "Give API access"}
              </DialogTitle>
              {/* The panel below is the explanation; the description only
                  names the API, for screen readers and for the fixed-producer
                  case where no picker says it. */}
              <DialogDescription className="break-words">
                {producerName ? `${producerName}'s` : "An agent's"} REST API
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4">
              <AgentApiAccessModeInfo
                mode={mode}
                onModeChange={setMode}
                disabled={isPending}
              />

              {!isProducerFixed && (
                <AgentApiProducerField
                  options={producerOptions}
                  selectedId={selectedProducerId}
                  onSelect={setSelectedProducerId}
                  isLoading={agentsQuery.isLoading}
                  isError={agentsQuery.isError}
                  error={agentsQuery.error}
                  onRetry={() => agentsQuery.refetch()}
                  active={open}
                  disabled={isPending}
                  externalAccessOffFor={
                    externalAccessOff ? producer : undefined
                  }
                />
              )}

              {isConnection ? (
                <AgentApiConnectionFields
                  scopes={scopes}
                  onScopesChange={handleScopesChange}
                  catalogScopes={catalogData?.scopes ?? []}
                  catalogEmpty={
                    catalogLoaded && (catalogData?.scopes ?? []).length === 0
                  }
                  scopesDisabled={!producerId}
                  scopesError={scopesError}
                  declaredScopes={declaredScopes}
                  catalogError={
                    catalogQuery.isError ? catalogQuery.error : undefined
                  }
                  onRetryCatalog={() => catalogQuery.refetch()}
                  name={label}
                  onNameChange={setLabel}
                  slot={slot}
                  onSlotChange={setSlot}
                  producerName={producerName}
                  disabled={isPending}
                />
              ) : (
                <AgentApiKeyFields
                  recipient={recipient}
                  onRecipientChange={setRecipient}
                  recipientIsMe={recipientIsMe}
                  scopes={scopes}
                  onScopesChange={setScopes}
                  catalogScopes={catalogData?.scopes ?? []}
                  expiry={expiry}
                  onExpiryChange={setExpiry}
                  label={label}
                  onLabelChange={setLabel}
                  slot={slot}
                  onSlotChange={setSlot}
                  producerName={producerName}
                  scopesDisabled={!producerId}
                  externalAccessOffFixed={externalAccessOff && isProducerFixed}
                  disabled={isPending}
                />
              )}
            </div>

            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={isPending}
              >
                Cancel
              </Button>
              <LoadingButton
                loading={isPending}
                disabled={!canSubmit}
                onClick={() =>
                  isConnection
                    ? connectMutation.mutate()
                    : createMutation.mutate()
                }
              >
                {isConnection ? "Create connection" : "Give access"}
              </LoadingButton>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
