import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { Bot } from "lucide-react"
import { useMemo, useState } from "react"

import type { AgentPublic } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
import { AgentApiScopeEditor } from "@/components/Common/AgentApiScopeEditor"
import {
  AgentSelectorDialog,
  type AgentOption,
} from "@/components/Common/AgentSelectorDialog"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useAuth from "@/hooks/useAuth"
import { agentApiKeysQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"
import { getColorPreset } from "@/utils/colorPresets"
import { AGENT_API_KEY_LABEL } from "@/components/Credentials/agentApiKeyCopy"
import { stashMintedAgentApiKey } from "@/components/Credentials/agentApiKeyMintHandoff"

interface AgentApiKeyDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * Pre-selected producer. Passed from the producer's own "Agent REST API"
   * card, where the agent is already the subject of the page; omitted in the
   * global credentials picker, which shows the agent selector instead.
   */
  producerAgentId?: string
}

/** Expiry choices, in days. ``null`` = never. */
const EXPIRY_OPTIONS: { value: string; label: string; days: number | null }[] = [
  { value: "never", label: "Never expires", days: null },
  { value: "7", label: "7 days", days: 7 },
  { value: "30", label: "30 days", days: 30 },
  { value: "90", label: "90 days", days: 90 },
  { value: "365", label: "1 year", days: 365 },
]

/**
 * Mint an agent-api **external key** — the form behind the global
 * "{AGENT_API_KEY_LABEL}" picker entry and the producer card's "Issue key"
 * button. Identical in both places by design (plan D10.1): the credentials
 * page's tabs must not change the form, only where the resulting credential
 * lands.
 *
 * Fields: producer agent → subject user → optional scopes → optional expiry.
 * The producer and subject use the same shared pickers as the rest of the app
 * (`AgentSelectorDialog`, `UserAllowlistPicker`), and the subject defaults to
 * the issuer. There is deliberately no per-key read-only switch: the producer's
 * `policy.yaml` is the primary read-only lever and already defaults to
 * read-only, so `read_only_override` is left at its backend default of false.
 *
 * Scopes are a convenience that upserts the ``(producer, subject)`` grant
 * server-side; they live there, never on the key (plan D5).
 *
 * On success we route to the new credential's detail page, which is where the
 * value is revealed and the curl snippet lives.
 */
export function AgentApiKeyDialog({
  open,
  onOpenChange,
  producerAgentId,
}: AgentApiKeyDialogProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { user: currentUser } = useAuth()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // The key's identity defaults to the person issuing it — the common case is
  // "a key for my own script". Any other user is one pick away.
  const selfAsSubject = useMemo<UserAllowlistSelectedItem | null>(
    () =>
      currentUser
        ? {
            id: currentUser.id,
            userId: currentUser.id,
            fallbackLabel: currentUser.full_name || currentUser.email,
          }
        : null,
    [currentUser],
  )

  const [selectedProducerId, setSelectedProducerId] = useState("")
  const [agentSelectorOpen, setAgentSelectorOpen] = useState(false)
  const [subject, setSubject] = useState<UserAllowlistSelectedItem | null>(
    selfAsSubject,
  )
  const [label, setLabel] = useState("")
  const [scopes, setScopes] = useState<string[]>([])
  const [expiry, setExpiry] = useState("never")

  // Reset on every open so a second visit never inherits the first one's
  // subject or scopes. Keyed on `open` so a close→open transition re-fires
  // (the same guard the Access & Scopes dialog uses). The subject resets to
  // self, not to empty — that is the default, not a blank slate.
  const [lastOpen, setLastOpen] = useState(false)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) {
      setSelectedProducerId("")
      setSubject(selfAsSubject)
      setLabel("")
      setScopes([])
      setExpiry("never")
    }
  }

  const isProducerFixed = !!producerAgentId
  const producerId = producerAgentId ?? selectedProducerId

  // Scope names are per-producer (they come from that agent's policy.yaml), so
  // switching producers must not carry a scope over to an API where it means
  // nothing — it would be written straight onto the grant.
  const [lastProducerId, setLastProducerId] = useState(producerId)
  if (producerId !== lastProducerId) {
    setLastProducerId(producerId)
    setScopes([])
  }

  const { data: agentsData } = useQuery({
    queryKey: ["agents", "agent-api-key-producers"],
    queryFn: () => AgentsService.readAgents({ limit: 200 }),
    enabled: open && !isProducerFixed,
  })

  // Only agents that expose a REST API AND are owned by the caller can be
  // producers — minting is owner-gated, so offering someone else's agent would
  // only ever produce a 404.
  const producers = useMemo<AgentPublic[]>(
    () =>
      (agentsData?.data ?? []).filter(
        (a) => a.agent_api_enabled && a.owner_id === currentUser?.id,
      ),
    [agentsData, currentUser?.id],
  )

  const selectedProducer = producers.find((a) => a.id === producerId)
  const selectedPreset = selectedProducer
    ? getColorPreset(selectedProducer.ui_color_preset)
    : null
  const producerOptions: AgentOption[] = producers.map((a) => ({
    id: a.id,
    name: a.name,
    colorPreset: a.ui_color_preset,
  }))

  // Scope catalog comes from the chosen producer's policy.yaml. It is owner-
  // gated, so it can only be fetched once a producer is picked.
  const { data: catalogData } = useQuery({
    queryKey: ["agentApiScopeCatalog", producerId],
    queryFn: () => AgentApiService.getAgentApiScopeCatalog({ agentId: producerId }),
    enabled: open && !!producerId,
  })

  const createMutation = useMutation({
    mutationFn: () =>
      AgentApiService.createAgentApiKey({
        agentId: producerId,
        requestBody: {
          label: label.trim() || null,
          subject_user_id: subject?.userId as string,
          // `null` leaves an existing grant untouched (plan D5) — only send a
          // scope set when the user actually chose one, so minting a second key
          // for someone never silently clears the scopes they already have.
          scopes: scopes.length > 0 ? scopes : null,
          expires_in_days:
            EXPIRY_OPTIONS.find((o) => o.value === expiry)?.days ?? null,
        },
      }),
    onSuccess: (created) => {
      showSuccessToast(`${AGENT_API_KEY_LABEL} created`)
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({
        queryKey: agentApiKeysQueryKey(producerId),
      })
      queryClient.invalidateQueries({ queryKey: ["agentApiGrants", producerId] })
      onOpenChange(false)
      if (created.credential_id) {
        // The mint response is the ONLY place this value comes from for free
        // (plan D4) — `with-data` no longer carries it, and going through the
        // reveal endpoint would audit a disclosure the user did not ask for.
        // Hand it to the detail page directly.
        stashMintedAgentApiKey(created.credential_id, created.token)
        // `new: 1` marks "just created" — the detail page latches it, reveals
        // the handed-off value once, and strips it from the URL so a refresh
        // does not re-reveal.
        navigate({
          to: "/credential/$credentialId",
          params: { credentialId: created.credential_id },
          search: { new: 1 },
        })
      }
    },
    onError: handleError.bind(showErrorToast),
  })

  // Only knowable when we loaded the agent list (the fixed-producer path is
  // reached from the producer card, which only offers the button when the
  // switch is on).
  const externalAccessOff =
    !!selectedProducer && !selectedProducer.agent_api_external_access_enabled
  const canSubmit =
    !!producerId && !!subject && !externalAccessOff && !createMutation.isPending

  return (
    <>
    {/* Rendered as a sibling, not a child: nesting a Dialog inside an open
        Dialog's content fights over focus trapping and the overlay. `agents`
        is passed explicitly so the picker offers the same owner-and-API-enabled
        set the form validates against, rather than re-fetching every agent. */}
    <AgentSelectorDialog
      open={agentSelectorOpen}
      onOpenChange={setAgentSelectorOpen}
      onSelect={setSelectedProducerId}
      selectedAgentId={selectedProducerId || null}
      agents={producerOptions}
      title="Select a producer agent"
    />
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{AGENT_API_KEY_LABEL}</DialogTitle>
          <DialogDescription>
            Issue a key so code outside the platform — a script, a server, a cron
            job — can call an agent's REST API. The key acts as one platform
            user, and you can revoke it at any time.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-1">
          {/* 1 — Producer agent */}
          <div className="space-y-2">
            <Label className="text-xs text-muted-foreground">
              Producer agent <span className="text-destructive">*</span>
            </Label>
            {isProducerFixed ? (
              <p className="text-xs text-muted-foreground">
                This agent's REST API.
              </p>
            ) : producers.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                None of your agents expose a REST API yet. Enable "Agent REST
                API" on an agent first.
              </p>
            ) : (
              // Same trigger-button + AgentSelectorDialog pattern the agentic-
              // teams AddNodeDialog uses, so an agent is picked the same way
              // everywhere — colour preset included, rather than a bare Select.
              <button
                type="button"
                onClick={() => setAgentSelectorOpen(true)}
                className={cn(
                  "flex w-full items-center gap-1.5 rounded-md px-3 py-2 text-left text-sm transition-all",
                  selectedPreset
                    ? `${selectedPreset.badgeBg} ${selectedPreset.badgeText} ${selectedPreset.badgeHover}`
                    : "bg-muted text-muted-foreground hover:bg-muted/80",
                )}
              >
                <Bot className="h-4 w-4 shrink-0" />
                <span className="truncate">
                  {selectedProducer?.name ??
                    "Pick an agent that exposes a REST API"}
                </span>
              </button>
            )}
            {externalAccessOff && (
              <p className="text-xs text-amber-600 dark:text-amber-500">
                External access is off for{" "}
                <span className="font-medium">{selectedProducer?.name}</span>.
                Turn on "External keys" on that agent's Agent REST API card
                first.
              </p>
            )}
          </div>

          {/* 2 — Subject user (the identity the key acts as) */}
          <div className="space-y-1.5">
            <UserAllowlistPicker
              enabled={open}
              includeSelf
              selected={subject ? [subject] : []}
              label={
                <Label className="text-xs text-muted-foreground">
                  Acts as <span className="text-destructive">*</span>
                </Label>
              }
              searchPlaceholder="Search users this key represents..."
              onAdd={(u) =>
                setSubject({
                  id: u.id,
                  userId: u.id,
                  fallbackLabel: u.full_name || u.email,
                })
              }
              onRemove={() => setSubject(null)}
            />
            <p className="text-xs text-muted-foreground">
              Every call made with this key is attributed to this user. Fixed
              once issued — changing it means revoking and re-issuing.
            </p>
          </div>

          {/* 3 — Scopes (optional) */}
          <div className="space-y-2">
            <Label className="text-xs text-muted-foreground">
              Scopes (optional)
            </Label>
            <AgentApiScopeEditor
              scopes={scopes}
              onChange={setScopes}
              catalogScopes={catalogData?.scopes ?? []}
              emptyHint="No scopes — the caller is identified but carries no capabilities."
              disabled={!producerId}
            />
            <p className="text-xs text-muted-foreground">
              Applied to this user on this producer, not to the key itself. Leave
              empty to keep whatever they already have.
            </p>
          </div>

          {/* 4 — Expiry. No read-only control: the producer's own policy.yaml
              is the primary read-only lever and already defaults to read-only,
              so a per-key narrowing knob is redundant here. The backend keeps
              defaulting `read_only_override` to false. */}
          <div className="space-y-2">
            <Label className="text-xs text-muted-foreground">Expires</Label>
            <Select value={expiry} onValueChange={setExpiry}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {EXPIRY_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Label — free text, defaults server-side to "<agent> API key". */}
          <div className="space-y-2">
            <Label className="text-xs text-muted-foreground">
              Name (optional)
            </Label>
            <Input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder={
                selectedProducer ? `${selectedProducer.name} API key` : "API key"
              }
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <LoadingButton
            loading={createMutation.isPending}
            disabled={!canSubmit}
            onClick={() => createMutation.mutate()}
          >
            Create key
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    </>
  )
}
