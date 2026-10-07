import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import {
  AlertCircle,
  Eye,
  KeyRound,
  PauseCircle,
  RefreshCw,
  Trash2,
} from "lucide-react"
import { useEffect, useState } from "react"

import type { AgentApiKeyHolderInfo, CredentialWithData } from "@/client"
import { CredentialsService } from "@/client"
import {
  formatRelativeTimestamp,
  parseTimestamp,
  RelativeTime,
} from "@/components/Common/RelativeTime"
import {
  AGENT_API_KEY_LABEL,
  agentApiKeyStatusLabel,
  agentApiKeyStatusSentence,
  isPausedAgentApiKeyStatus,
  isTerminalAgentApiKeyStatus,
} from "@/components/Credentials/agentApiKeyCopy"
import { takeMintedAgentApiKey } from "@/components/Credentials/agentApiKeyMintHandoff"
import { AgentApiKeyStatusBadge } from "@/components/Credentials/AgentApiKeyStatusBadge"
import { AgentApiKeyUsage } from "@/components/Credentials/AgentApiKeyUsage"
import DeleteCredential from "@/components/Credentials/DeleteCredential"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { LoadingButton } from "@/components/ui/loading-button"
import { agentApiKeyHolderQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { openAgentApiKeySpec } from "@/utils/agentApiSpec"

interface AgentApiKeyTopCardProps {
  credential: CredentialWithData
  holder: AgentApiKeyHolderInfo
  /** ``?new=1``: the value was just issued and is waiting in the handoff. */
  justCreated: boolean
}

/**
 * The holder's key card: is it working, the value and how to call with it,
 * and Rotate. At most one state `Alert` leads the body, by precedence —
 * ended > paused > "the issuer has seen this value" — so a key never shows two
 * competing banners. An ended key is the header and that Alert only: its value,
 * curl and rotate are gone because none of them can do anything any more.
 *
 * Keyed on the credential by the parent, and load-bearing: the route does NOT
 * remount when `credentialId` changes, and this card holds a revealed value.
 */
export function AgentApiKeyTopCard({
  credential,
  holder,
  justCreated,
}: AgentApiKeyTopCardProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  // `null` = masked. The value only ever lives here, while it is on screen.
  const [token, setToken] = useState<string | null>(null)
  const [rotateOpen, setRotateOpen] = useState(false)

  // Claim the just-issued value, if the issue flow parked one. Always drains
  // (one-shot) but only displays when the route says this is the step right
  // after issuing. No `else setToken(null)`: StrictMode runs this twice and
  // the second pass finds the slot already drained.
  useEffect(() => {
    const minted = takeMintedAgentApiKey(credential.id)
    if (minted && justCreated) setToken(minted)
  }, [credential.id, justCreated])

  const status = holder.status
  const terminal = isTerminalAgentApiKeyStatus(status)
  const paused = isPausedAgentApiKeyStatus(status)
  const producerName = holder.producer?.name || null
  const issuerLabel = holder.issuer?.full_name || holder.issuer?.email || null
  const sentence = agentApiKeyStatusSentence(status, {
    issuerLabel,
    expiredRelative: holder.expires_at
      ? formatRelativeTimestamp(holder.expires_at)
      : null,
  })
  // The base URL the holder calls — the holder view's, else the one stored
  // on the credential at issue. Always the PUBLIC origin.
  const baseUrl =
    holder.base_url ||
    (credential.credential_data?.base_url as string | undefined) ||
    ""

  // The overlap line shows only while the previous value still works, and
  // goes away on its own when the window ends (not "stops working 1 minute
  // ago" on a page left open).
  const graceEndsAt = holder.previous_valid_until
    ? (parseTimestamp(holder.previous_valid_until)?.getTime() ?? 0)
    : 0
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const remaining = graceEndsAt - Date.now()
    if (remaining <= 0) return
    const handle = setTimeout(() => setNow(Date.now()), remaining + 500)
    return () => clearTimeout(handle)
  }, [graceEndsAt])
  const graceRunning = graceEndsAt > Math.max(now, Date.now())
  const issuerSawAlert = !terminal && !paused && holder.issuer_saw_value

  const rotateMutation = useMutation({
    mutationFn: () => CredentialsService.rotateAgentApiKey({ id: credential.id }),
    onSuccess: (data) => {
      // The response is the new value: show it, no reveal call, no extra
      // audit event for a value the user was just handed.
      setToken(data.token)
      setRotateOpen(false)
      showSuccessToast("Key rotated")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({
        queryKey: agentApiKeyHolderQueryKey(credential.id),
      })
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
    },
  })

  const rotateButton = () => (
    <Button
      variant="outline"
      size="sm"
      onClick={() => setRotateOpen(true)}
      disabled={rotateMutation.isPending}
    >
      <RefreshCw className="h-4 w-4 mr-1.5" />
      Rotate key
    </Button>
  )

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <KeyRound className="h-5 w-5" />
          <span className="truncate">{AGENT_API_KEY_LABEL}</span>
          <AgentApiKeyStatusBadge status={status} />
        </CardTitle>
        <CardDescription>
          Lets code outside the platform call{" "}
          {producerName ? `${producerName}'s` : "an agent's"} REST API as you.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {terminal ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>This key no longer works</AlertTitle>
            <AlertDescription className="space-y-3">
              <p className="break-words">{sentence}</p>
              <DeleteCredential
                credential={credential}
                onSuccess={() => navigate({ to: "/credentials" })}
              >
                <Button variant="destructive" size="sm">
                  <Trash2 className="h-4 w-4 mr-1.5" />
                  Delete key
                </Button>
              </DeleteCredential>
            </AlertDescription>
          </Alert>
        ) : paused ? (
          <Alert>
            <PauseCircle className="h-4 w-4" />
            <AlertTitle>{agentApiKeyStatusLabel(status)}</AlertTitle>
            <AlertDescription>
              <p className="break-words">{sentence}</p>
            </AlertDescription>
          </Alert>
        ) : holder.issuer_saw_value ? (
          <Alert>
            <Eye className="h-4 w-4" />
            <AlertTitle>The issuer has seen this value</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>Rotate it so only you hold it.</p>
              {rotateButton()}
            </AlertDescription>
          </Alert>
        ) : null}

        {!terminal && (
          <>
            <AgentApiKeyUsage
              credentialId={credential.id}
              tokenPrefix={holder.token_prefix}
              baseUrl={baseUrl}
              token={token}
              onTokenChange={setToken}
              onViewSpec={
                holder.producer
                  ? () =>
                      openAgentApiKeySpec(
                        holder.producer?.id as string,
                        credential.id,
                      )
                  : undefined
              }
            />

            <div className="space-y-1.5">
              {/* While the issuer-saw Alert is up it carries Rotate; one
                  button per card. */}
              {!issuerSawAlert && rotateButton()}
              {graceRunning && holder.previous_valid_until && (
                <p className="text-xs text-muted-foreground">
                  The previous value stops working{" "}
                  <RelativeTime
                    timestamp={holder.previous_valid_until}
                    showTooltip
                  />
                  .
                </p>
              )}
            </div>
          </>
        )}
      </CardContent>

      <AlertDialog
        open={rotateOpen}
        // Escape would otherwise close the confirm mid-request.
        onOpenChange={(next) => {
          if (!rotateMutation.isPending) setRotateOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Rotate this key?</AlertDialogTitle>
            <AlertDialogDescription>
              You get a new value now. The current value keeps working for 5
              minutes so running code can switch over, then stops.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={rotateMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <LoadingButton
              loading={rotateMutation.isPending}
              onClick={() => rotateMutation.mutate()}
            >
              {rotateMutation.isPending ? "Rotating…" : "Rotate key"}
            </LoadingButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
