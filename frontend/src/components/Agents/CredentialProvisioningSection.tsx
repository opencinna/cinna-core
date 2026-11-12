/**
 * CredentialProvisioningSection — Phase 5 of the install-experience
 * redesign.
 *
 * Surfaces two controls on the publisher install's bundle tab:
 *
 *   1. Per linked credential — a "User provides" / "Embedded (shared)" /
 *      "Template" dropdown that auto-saves on change. Persists into the
 *      install's ``publish_settings.credential_overrides`` JSON map. The value
 *      shown, and which choices are open, come from the server
 *      (``getBundleCredentialDrift().live``): the same resolver and re-use
 *      policy publish applies, so a credential shared *with* the publisher is
 *      embeddable only when its owner allows re-use inside bundles.
 *
 *   2. AI credential pickers (Conversation + Building) showing the
 *      currently used SDK engine. The dropdown is filtered by the
 *      compatibility matrix (``claude-code`` only sees ``anthropic``
 *      credentials, etc.). Choosing "None — user provides"
 *      clears the bundle-level FK; foreign installs revert to user
 *      provides AI.
 *
 * Rendered for an agent that can publish (see ``canPublishFrom``): the
 * publisher install, or a never-published agent whose first publish reads
 * the overrides saved here. Before the first publish ``bundle`` is
 * undefined and there is nothing to drift from, so no "republish" hint.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import {
  AlertTriangle,
  Hammer,
  Info,
  KeyRound,
  MessageCircle,
} from "lucide-react"
import { useEffect, useMemo } from "react"

import {
  type AgentBundlePublic,
  type AgentPublic,
  AgentsService,
  AiCredentialsService,
  BundlesService,
  EnvironmentsService,
  InstallsService,
} from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { CredentialTypeBadge } from "@/components/Credentials/CredentialTypeBadge"
import {
  type ProvidedBy,
  providedByLabel,
} from "@/components/Credentials/providedByLabel"
import {
  extractEngine,
  getEngineLabel,
  SDK_CREDENTIAL_COMPATIBILITY,
  sdkExpectedCredentialType,
} from "@/components/Environments/EnvironmentConfigForm"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { reuseBlockedSentence } from "@/utils/credentialBundleReuse"

/**
 * Mirrors ``PublishService.can_publish_from`` on the backend: a publisher
 * install, or an agent of the owner's that has never been published (no
 * bundle yet). A foreign install (bundle set, not the publisher) never is.
 */
export function canPublishFrom(agent: AgentPublic): boolean {
  return !!agent.is_publisher_install || !agent.bundle_uuid
}

interface CredentialProvisioningSectionProps {
  agent: AgentPublic
  bundle: AgentBundlePublic | undefined
}

export function CredentialProvisioningSection({
  agent,
  bundle,
}: CredentialProvisioningSectionProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const canPublish = canPublishFrom(agent)

  // ── Data ────────────────────────────────────────────────────────────

  const {
    data: agentCredentials,
    isLoading: isAgentCredentialsLoading,
    isError: isAgentCredentialsError,
    error: agentCredentialsError,
    refetch: refetchAgentCredentials,
  } = useQuery({
    queryKey: ["agent-credentials", agent.id],
    queryFn: () => AgentsService.readAgentCredentials({ id: agent.id }),
    enabled: canPublish,
  })

  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentials"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
    enabled: canPublish,
  })

  // Live-vs-published ``provided_by`` drift. Installers receive the value
  // frozen into the latest published revision; this screen recomputes it
  // live. When they diverge (the publisher changed sharing after publishing)
  // we surface a "republish to apply" hint per drifted credential.
  const {
    data: drift,
    isError: isDriftError,
    error: driftError,
    refetch: refetchDrift,
  } = useQuery({
    queryKey: ["bundle-credential-drift", agent.id],
    queryFn: () =>
      InstallsService.getBundleCredentialDrift({ agentId: agent.id }),
    enabled: canPublish,
  })

  const driftByName = useMemo<
    Record<string, { live: ProvidedBy; snapshot: ProvidedBy }>
  >(() => {
    const out: Record<string, { live: ProvidedBy; snapshot: ProvidedBy }> = {}
    for (const d of drift?.drift ?? []) {
      if (d.drifted) {
        out[d.name] = {
          live: d.live_provided_by,
          snapshot: d.snapshot_provided_by,
        }
      }
    }
    return out
  }, [drift])

  const { data: environment } = useQuery({
    queryKey: ["environment", agent.active_environment_id],
    queryFn: () =>
      EnvironmentsService.getEnvironment({
        id: agent.active_environment_id as string,
      }),
    enabled: canPublish && !!agent.active_environment_id,
  })

  const linkedCredentials = agentCredentials?.data ?? []
  const aiCredentialOptions = aiCredentials?.data ?? []

  // SDK engine per mode (claude-code / opencode), derived from the env's
  // SDK ID. Used to display which SDK is currently in use.
  const conversationEngine = extractEngine(environment?.agent_sdk_conversation)
  const buildingEngine = extractEngine(environment?.agent_sdk_building)

  // Strict provider match — the dropdown only offers credentials whose
  // ``type`` equals the SDK's expected provider (e.g. ``opencode/anthropic``
  // → ``anthropic`` only, never ``openai``). Backend rejects the mismatch
  // at PATCH /bundles and at publish time; matching the filter here keeps
  // the UI from suggesting choices that would fail server-side.
  const conversationExpectedType = sdkExpectedCredentialType(
    environment?.agent_sdk_conversation,
  )
  const buildingExpectedType = sdkExpectedCredentialType(
    environment?.agent_sdk_building,
  )
  // Fallback to engine compatibility for SDK strings the strict map doesn't
  // know yet (e.g. custom provider suffixes) so we don't accidentally show
  // an empty dropdown.
  const conversationCompatibleTypes = conversationExpectedType
    ? [conversationExpectedType]
    : (SDK_CREDENTIAL_COMPATIBILITY[conversationEngine] ?? [])
  const buildingCompatibleTypes = buildingExpectedType
    ? [buildingExpectedType]
    : (SDK_CREDENTIAL_COMPATIBILITY[buildingEngine] ?? [])

  const conversationOptions = aiCredentialOptions.filter((c) =>
    conversationCompatibleTypes.includes(c.type),
  )
  const buildingOptions = aiCredentialOptions.filter((c) =>
    buildingCompatibleTypes.includes(c.type),
  )

  // ── Server overrides + inference ────────────────────────────────────

  const serverOverrides = useMemo<Record<string, ProvidedBy>>(() => {
    const raw =
      (
        agent.publish_settings as
          | { credential_overrides?: Record<string, { provided_by?: string }> }
          | undefined
      )?.credential_overrides ?? {}
    const out: Record<string, ProvidedBy> = {}
    for (const [name, entry] of Object.entries(raw)) {
      if (
        entry?.provided_by === "user" ||
        entry?.provided_by === "publisher" ||
        entry?.provided_by === "template"
      ) {
        out[name] = entry.provided_by
      }
    }
    return out
  }, [agent.publish_settings])

  // Server-computed provisioning value per linked credential: the same
  // resolver and re-use policy publish applies (a credential shared with the
  // publisher is not embeddable unless its owner allows re-use). Never
  // inferred client-side — that inference was the P0 bug.
  const liveById = useMemo(
    () =>
      new Map(
        (drift?.live ?? []).map((option) => [option.credential_id, option]),
      ),
    [drift],
  )

  // Pre-publish AI credential draft — stored on the publisher install's
  // ``publish_settings.ai_credentials`` until the bundle row exists. After
  // first publish the bundle FK columns are the source of truth.
  const draftAiCredentials = useMemo<{
    conversation_credential_id: string | null
    building_credential_id: string | null
  }>(() => {
    const raw =
      (
        agent.publish_settings as
          | {
              ai_credentials?: {
                conversation_credential_id?: string | null
                building_credential_id?: string | null
              }
            }
          | undefined
      )?.ai_credentials ?? {}
    return {
      conversation_credential_id: raw.conversation_credential_id ?? null,
      building_credential_id: raw.building_credential_id ?? null,
    }
  }, [agent.publish_settings])

  const conversationAiId = bundle
    ? (bundle.publisher_ai_credential_conversation_id ?? null)
    : draftAiCredentials.conversation_credential_id
  const buildingAiId = bundle
    ? (bundle.publisher_ai_credential_building_id ?? null)
    : draftAiCredentials.building_credential_id

  // A credential linked after the drift was read has no server value yet:
  // its select stays a skeleton, and the drift is read again once.
  const missingLive =
    !!drift && linkedCredentials.some((cred) => !liveById.has(cred.id))
  useEffect(() => {
    if (missingLive) refetchDrift()
  }, [missingLive, refetchDrift])

  // ── Mutations ───────────────────────────────────────────────────────

  const savePublishSettingsMutation = useMutation({
    mutationFn: (nextOverrides: Record<string, ProvidedBy>) =>
      InstallsService.updatePublishSettings({
        agentId: agent.id,
        requestBody: {
          credential_overrides: Object.fromEntries(
            Object.entries(nextOverrides).map(([name, providedBy]) => [
              name,
              { provided_by: providedBy },
            ]),
          ),
        },
      }),
    onSuccess: () => {
      showSuccessToast("Credential override saved")
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["bundles"] })
      queryClient.invalidateQueries({
        queryKey: ["bundle-credential-drift", agent.id],
      })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to save credential override")
    },
  })

  // Post-publish: write directly to the bundle FK columns.
  const updateBundleAiMutation = useMutation({
    mutationFn: (patch: {
      publisher_ai_credential_conversation_id?: string | null
      publisher_ai_credential_building_id?: string | null
    }) =>
      BundlesService.updateBundle({
        bundleUuid: agent.bundle_uuid as string,
        requestBody: patch,
      }),
    onSuccess: () => {
      showSuccessToast("AI credential saved")
      queryClient.invalidateQueries({
        queryKey: ["bundles", agent.bundle_uuid],
      })
      queryClient.invalidateQueries({ queryKey: ["bundles"] })
      queryClient.invalidateQueries({ queryKey: ["catalog"] })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to update AI credentials")
    },
  })

  // Pre-publish: persist on the publisher install's publish_settings draft.
  const updateDraftAiMutation = useMutation({
    mutationFn: (next: {
      conversation_credential_id: string | null
      building_credential_id: string | null
    }) =>
      InstallsService.updatePublishSettings({
        agentId: agent.id,
        requestBody: { ai_credentials: next },
      }),
    onSuccess: () => {
      showSuccessToast("AI credential saved (will apply on first publish)")
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to save AI credentials")
    },
  })

  const handleOverrideChange = (credName: string, providedBy: ProvidedBy) => {
    const next: Record<string, ProvidedBy> = {
      ...serverOverrides,
      [credName]: providedBy,
    }
    savePublishSettingsMutation.mutate(next)
  }

  const handlePickAi = (
    mode: "conversation" | "building",
    credentialId: string | null,
  ) => {
    if (bundle) {
      const key =
        mode === "conversation"
          ? "publisher_ai_credential_conversation_id"
          : "publisher_ai_credential_building_id"
      updateBundleAiMutation.mutate({ [key]: credentialId })
      return
    }
    // Pre-publish: persist as a draft on the install's publish_settings.
    // The publish flow transfers these onto the new bundle row at first
    // publish time.
    const next = {
      conversation_credential_id: draftAiCredentials.conversation_credential_id,
      building_credential_id: draftAiCredentials.building_credential_id,
    }
    if (mode === "conversation") next.conversation_credential_id = credentialId
    else next.building_credential_id = credentialId
    updateDraftAiMutation.mutate(next)
  }

  // ── Render ──────────────────────────────────────────────────────────

  if (!canPublish) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 min-w-0">
          <KeyRound className="h-5 w-5" />
          Credential provisioning
        </CardTitle>
        <CardDescription>
          Decide which credentials you (the publisher) provide for foreign
          installs and which the user must supply themselves.{" "}
          {bundle
            ? "These choices are baked into the next published revision."
            : "These choices are saved now and applied when you first publish."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {isDriftError && !drift && linkedCredentials.length > 0 && (
          <QueryErrorAlert
            error={driftError}
            fallback="Couldn't check which credentials you can embed"
            onRetry={() => refetchDrift()}
            compact
          />
        )}
        {/* Per-credential override map. */}
        {isAgentCredentialsLoading ? (
          <Skeleton className="h-[48px] w-full rounded-md" />
        ) : isAgentCredentialsError && !agentCredentials ? (
          <QueryErrorAlert
            error={agentCredentialsError}
            fallback="Couldn't load the linked credentials"
            onRetry={() => refetchAgentCredentials()}
            compact
          />
        ) : linkedCredentials.length === 0 ? (
          <p className="text-sm text-muted-foreground py-2">
            No service credentials linked to this agent yet — link credentials
            from the Credentials tab to manage their provisioning here.
          </p>
        ) : (
          linkedCredentials.map((cred) => {
            const live = liveById.get(cred.id)
            const canEmbed = !!live?.can_provide_as?.includes("publisher")
            const canTemplate = !!live?.can_provide_as?.includes("template")
            const reason = live
              ? reuseBlockedSentence(
                  live.blocked_reason,
                  cred.owner_email,
                  !cred.is_shared,
                )
              : null
            // A stale explicit "Embedded" override publish would reject.
            const willFail =
              !!live && live.resolved_provided_by === "publisher" && !canEmbed
            // Nothing to republish before the first publish.
            const rowDrift =
              willFail || !bundle ? undefined : driftByName[cred.name]
            return (
              <div key={cred.id} className="space-y-2 py-2">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 space-y-1.5">
                    <CredentialTypeBadge type={cred.type} />
                    <div className="text-xs text-muted-foreground flex items-center gap-2 flex-wrap">
                      <span>detected from</span>
                      <Badge asChild variant="outline">
                        <Link
                          to="/credential/$credentialId"
                          params={{ credentialId: cred.id }}
                        >
                          {cred.name}
                        </Link>
                      </Badge>
                    </div>
                    {/* Why "Embedded (shared)" is closed. Muted: "User
                        provides" is a valid answer, nothing is wrong. Not in
                        the disabled item, whose children would also print in
                        the trigger. */}
                    {!canEmbed && !willFail && reason && (
                      <p className="flex items-start gap-1 text-xs text-muted-foreground">
                        <Info className="mt-0.5 h-3 w-3 shrink-0" />
                        <span className="min-w-0 break-words">{reason}</span>
                      </p>
                    )}
                  </div>
                  {!live ? (
                    // Never a client-inferred value while the server's loads.
                    <Skeleton className="h-9 w-[260px] shrink-0" />
                  ) : (
                    <Select
                      value={live.resolved_provided_by}
                      onValueChange={(val) =>
                        handleOverrideChange(cred.name, val as ProvidedBy)
                      }
                      disabled={savePublishSettingsMutation.isPending}
                    >
                      <SelectTrigger className="w-[260px] shrink-0">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="user">User provides</SelectItem>
                        <SelectItem value="publisher" disabled={!canEmbed}>
                          Embedded (shared)
                        </SelectItem>
                        {cred.type !== "agent_api" && (
                          <SelectItem value="template" disabled={!canTemplate}>
                            Template (defaults + private)
                          </SelectItem>
                        )}
                      </SelectContent>
                    </Select>
                  )}
                </div>
                {willFail && (
                  <Alert className="border-warning text-warning">
                    <AlertTriangle />
                    <AlertTitle>Publishing will fail</AlertTitle>
                    <AlertDescription>
                      {reason ? `${reason} ` : ""}Choose "User provides".
                    </AlertDescription>
                  </Alert>
                )}
                {/* Full row width, below the badges and the select: inside the
                    left column the fixed-width select squeezed it into a
                    one-word-per-line strip. The house warning `Alert`, like the
                    Revisions card's "Republish to apply credential sharing
                    changes" — the same fact, seen from this credential's row.
                    Title and body say different things: what installers get
                    now, and what to do about it. */}
                {rowDrift && (
                  <Alert className="border-warning text-warning">
                    <AlertTriangle />
                    <AlertTitle>
                      Installers still get "{providedByLabel(rowDrift.snapshot)}
                      "
                    </AlertTitle>
                    <AlertDescription>
                      Republish the bundle to apply "
                      {providedByLabel(rowDrift.live)}".
                    </AlertDescription>
                  </Alert>
                )}
              </div>
            )
          })
        )}

        {/* AI credentials. */}
        <div className="pt-2">
          <Label className="text-sm font-medium">AI credentials</Label>
          <p className="text-xs text-muted-foreground">
            Pick an AI credential to share with foreign installs, or leave "None
            — user provides" so the user supplies their own. Only credentials
            compatible with the mode's SDK are listed.
          </p>
        </div>
        {(() => {
          const aiPending =
            updateBundleAiMutation.isPending || updateDraftAiMutation.isPending
          return (
            <>
              {/* Conversation mode */}
              <div className="flex items-start justify-between gap-4 py-2">
                <div className="min-w-0 flex items-start gap-2">
                  <MessageCircle className="h-4 w-4 text-muted-foreground shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <Label className="text-sm font-medium">
                      Conversation AI
                    </Label>
                    <p className="text-xs text-muted-foreground">
                      SDK in use: {getEngineLabel(conversationEngine)}
                    </p>
                  </div>
                </div>
                <Select
                  value={conversationAiId ?? "__none__"}
                  onValueChange={(val) =>
                    handlePickAi(
                      "conversation",
                      val === "__none__" ? null : val,
                    )
                  }
                  disabled={aiPending}
                >
                  <SelectTrigger className="w-[260px] shrink-0">
                    <SelectValue placeholder="Select an AI credential" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">
                      None — user provides
                    </SelectItem>
                    {conversationOptions.map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        {c.name} ({c.type})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {/* Building mode */}
              <div className="flex items-start justify-between gap-4 py-2">
                <div className="min-w-0 flex items-start gap-2">
                  <Hammer className="h-4 w-4 text-muted-foreground shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <Label className="text-sm font-medium">Building AI</Label>
                    <p className="text-xs text-muted-foreground">
                      SDK in use: {getEngineLabel(buildingEngine)}
                    </p>
                  </div>
                </div>
                <Select
                  value={buildingAiId ?? "__none__"}
                  onValueChange={(val) =>
                    handlePickAi("building", val === "__none__" ? null : val)
                  }
                  disabled={aiPending}
                >
                  <SelectTrigger className="w-[260px] shrink-0">
                    <SelectValue placeholder="Select an AI credential" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">
                      None — user provides
                    </SelectItem>
                    {buildingOptions.map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        {c.name} ({c.type})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </>
          )
        })()}
      </CardContent>
    </Card>
  )
}
