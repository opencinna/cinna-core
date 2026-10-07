import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import { AlertTriangle, CheckCircle2 } from "lucide-react"
import { useMemo, useState } from "react"

import type { AgentPublic } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
import { AgentApiScopeEditor } from "@/components/Common/AgentApiScopeEditor"
import {
  type AgentOption,
  AgentSelectorList,
} from "@/components/Common/AgentSelectorDialog"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  UserSelectField,
  type UserSelectValue,
} from "@/components/Common/UserSelectField"
import {
  AgentApiKeyAdvancedFields,
  expiryDays,
} from "@/components/Credentials/AgentApiKeyAdvancedFields"
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
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import useAuth from "@/hooks/useAuth"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
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
}

/** A key issued to someone else: the body swaps to this, no navigation. */
interface IssuedToOther {
  recipientName: string
  keyLabel: string
  producerName: string
}

/**
 * "Give API access" — issue a key on a producer to myself or to a colleague.
 * Behind the producer's API access card ("Give access", "Give another key")
 * and `/credentials` › New › {AGENT_API_KEY_LABEL}; the same form in both, the
 * second only adds the producer picker.
 *
 * The key lands in the **recipient's** credentials. Issued to me, the flow
 * ends where the value is (the detail page, value shown once from the issue
 * response). Issued to someone else, the issuer never sees the value
 * (``token`` is ``null``), so the dialog ends on a success panel instead.
 *
 * Scopes are a convenience that upserts the ``(producer, recipient)`` grant;
 * they live there, never on the key. Expiry, name and slot have defaults and
 * wait behind one Advanced disclosure. The slot is sent ``null`` when left
 * empty so the server's default (plan D23) stays the one source of the slug;
 * the placeholder only shows it.
 */
export function AgentApiKeyDialog({
  open,
  onOpenChange,
  producerAgentId,
  initialRecipient,
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

  const [selectedProducerId, setSelectedProducerId] = useState("")
  const [recipient, setRecipient] = useState<UserSelectValue | null>(null)
  const [label, setLabel] = useState("")
  const [slot, setSlot] = useState("")
  const [scopes, setScopes] = useState<string[]>([])
  const [expiry, setExpiry] = useState("never")
  const [issued, setIssued] = useState<IssuedToOther | null>(null)

  // Reset on every open so a second visit never inherits the first one's
  // recipient or scopes. Keyed on `open` so a close→open transition re-fires.
  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) {
      setSelectedProducerId("")
      setRecipient(initialRecipient ?? me)
      setLabel("")
      setSlot("")
      setScopes([])
      setExpiry("never")
      setIssued(null)
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

  const { data: catalogData } = useQuery({
    queryKey: ["agentApiScopeCatalog", producerId],
    queryFn: () =>
      AgentApiService.getAgentApiScopeCatalog({ agentId: producerId }),
    enabled: open && !!producerId,
  })

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

  // Only knowable on the picker path: the producer card disables its button
  // while external access is off.
  const externalAccessOff =
    !isProducerFixed &&
    !!producer &&
    !producer.agent_api_external_access_enabled
  const canSubmit =
    !!producerId &&
    !!recipient &&
    !externalAccessOff &&
    !createMutation.isPending

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape / outside-click do nothing while the key is being issued.
        if (createMutation.isPending) return
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-lg [&>*]:min-w-0">
        {issued ? (
          <>
            <DialogHeader>
              <DialogTitle>Give API access</DialogTitle>
            </DialogHeader>
            <div className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
              <div className="min-w-0 space-y-1">
                <p className="text-sm font-medium break-words">
                  {issued.recipientName} has a new key
                </p>
                <p className="text-sm text-muted-foreground break-words">
                  It is in their credentials as {issued.keyLabel}. You won't see
                  its value. Revoke it from {issued.producerName}'s API access
                  card.
                </p>
              </div>
            </div>
            <DialogFooter>
              <Button onClick={() => onOpenChange(false)}>Done</Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Give API access</DialogTitle>
              <DialogDescription>
                Issue a key so code outside the platform can call{" "}
                {producerName ? `${producerName}'s` : "an agent's"} REST API.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4">
              {!isProducerFixed && (
                <fieldset className="space-y-1.5">
                  <Label asChild>
                    <legend>Producer</legend>
                  </Label>
                  {agentsQuery.isError ? (
                    <QueryErrorAlert
                      error={agentsQuery.error}
                      fallback="Couldn't load your agents"
                      onRetry={() => agentsQuery.refetch()}
                      compact
                    />
                  ) : agentsQuery.isLoading ? (
                    <Skeleton className="h-9 w-full rounded-md" />
                  ) : producers.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      None of your agents expose a REST API yet.
                    </p>
                  ) : (
                    <AgentSelectorList
                      mode="single"
                      agents={producerOptions}
                      selectedAgentId={selectedProducerId || null}
                      onSelect={setSelectedProducerId}
                      placeholder="Pick an agent that exposes a REST API"
                      active={open}
                      disabled={createMutation.isPending}
                    />
                  )}
                  {externalAccessOff && producer && (
                    <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
                      <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" />
                      <span>
                        External access is off for {producer.name}. Turn it on
                        in its{" "}
                        <Link
                          to="/agent/$agentId"
                          params={{ agentId: producer.id }}
                          hash="integrations"
                          className="text-primary hover:underline"
                        >
                          API access card
                        </Link>{" "}
                        first.
                      </span>
                    </p>
                  )}
                </fieldset>
              )}

              <div className="space-y-1.5">
                <Label htmlFor="agent-api-key-recipient">Issued to</Label>
                <UserSelectField
                  id="agent-api-key-recipient"
                  value={recipient}
                  onChange={setRecipient}
                  includeSelf
                  disabled={createMutation.isPending}
                />
                {recipient && !recipientIsMe && (
                  <p className="text-xs text-muted-foreground break-words">
                    The key goes to {recipientName}'s credentials. You won't see
                    its value, and you can revoke it anytime.
                  </p>
                )}
              </div>

              <div className="space-y-1.5">
                <Label>Scopes (optional)</Label>
                <AgentApiScopeEditor
                  scopes={scopes}
                  onChange={setScopes}
                  catalogScopes={catalogData?.scopes ?? []}
                  emptyHint="No scopes — the caller is identified but carries no capabilities."
                  disabled={!producerId || createMutation.isPending}
                />
                <p className="text-xs text-muted-foreground">
                  {recipientIsMe || !recipient
                    ? "Shared by every key you hold on this agent. Leave empty to keep what you have."
                    : `Shared by every key ${recipientName} holds on this agent. Leave empty to keep what they have.`}
                </p>
              </div>

              <AgentApiKeyAdvancedFields
                expiry={expiry}
                onExpiryChange={setExpiry}
                label={label}
                onLabelChange={setLabel}
                slot={slot}
                onSlotChange={setSlot}
                producerName={producerName}
              />
            </div>

            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={createMutation.isPending}
              >
                Cancel
              </Button>
              <LoadingButton
                loading={createMutation.isPending}
                disabled={!canSubmit}
                onClick={() => createMutation.mutate()}
              >
                Give access
              </LoadingButton>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
