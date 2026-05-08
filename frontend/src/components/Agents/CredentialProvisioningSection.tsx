/**
 * CredentialProvisioningSection — Phase 5 of the install-experience
 * redesign.
 *
 * Surfaces two controls on the publisher install's bundle tab:
 *
 *   1. Per linked credential — a "User provides" / "I provide" dropdown
 *      that auto-saves on change. Persists into the install's
 *      ``publish_settings.credential_overrides`` JSON map. Empty = inferred
 *      from the credential's ``allow_sharing`` at publish time.
 *
 *   2. AI credential pickers (Conversation + Building) showing the
 *      currently used SDK engine. The dropdown is filtered by the
 *      compatibility matrix (``claude-code`` only sees ``anthropic`` /
 *      ``minimax`` credentials, etc.). Choosing "None — user provides"
 *      clears the bundle-level FK; foreign installs revert to user
 *      provides AI.
 *
 * Only rendered for the publisher install (``agent.is_publisher_install
 * === true``).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useMemo } from "react"
import { Link } from "@tanstack/react-router"
import { AlertTriangle, Hammer, MessageCircle } from "lucide-react"

import {
  AgentsService,
  AiCredentialsService,
  BundlesService,
  EnvironmentsService,
  InstallsService,
  type AgentPublic,
  type AgentBundlePublic,
} from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { CredentialTypeBadge } from "@/components/Credentials/CredentialTypeBadge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  SDK_CREDENTIAL_COMPATIBILITY,
  extractEngine,
  getEngineLabel,
} from "@/components/Environments/EnvironmentConfigForm"

interface CredentialProvisioningSectionProps {
  agent: AgentPublic
  bundle: AgentBundlePublic | undefined
}

type ProvidedBy = "user" | "publisher"

export function CredentialProvisioningSection({
  agent,
  bundle,
}: CredentialProvisioningSectionProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // ── Data ────────────────────────────────────────────────────────────

  const { data: agentCredentials } = useQuery({
    queryKey: ["agent-credentials", agent.id],
    queryFn: () => AgentsService.readAgentCredentials({ id: agent.id }),
    enabled: agent.is_publisher_install,
  })

  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentials"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
    enabled: agent.is_publisher_install,
  })

  const { data: environment } = useQuery({
    queryKey: ["environment", agent.active_environment_id],
    queryFn: () =>
      EnvironmentsService.getEnvironment({
        id: agent.active_environment_id as string,
      }),
    enabled: agent.is_publisher_install && !!agent.active_environment_id,
  })

  const linkedCredentials = agentCredentials?.data ?? []
  const aiCredentialOptions = aiCredentials?.data ?? []

  // SDK engine per mode (claude-code / opencode), derived from the env's
  // SDK ID. Used to (a) display which SDK is currently in use, (b) filter
  // the AI credential dropdown to only types compatible with that SDK.
  const conversationEngine = extractEngine(environment?.agent_sdk_conversation)
  const buildingEngine = extractEngine(environment?.agent_sdk_building)

  const conversationCompatibleTypes =
    SDK_CREDENTIAL_COMPATIBILITY[conversationEngine] ?? []
  const buildingCompatibleTypes =
    SDK_CREDENTIAL_COMPATIBILITY[buildingEngine] ?? []

  const conversationOptions = aiCredentialOptions.filter((c) =>
    conversationCompatibleTypes.includes(c.type),
  )
  const buildingOptions = aiCredentialOptions.filter((c) =>
    buildingCompatibleTypes.includes(c.type),
  )

  // ── Server overrides + inference ────────────────────────────────────

  const serverOverrides = useMemo<Record<string, ProvidedBy>>(() => {
    const raw =
      (agent.publish_settings as
        | { credential_overrides?: Record<string, { provided_by?: string }> }
        | undefined)?.credential_overrides ?? {}
    const out: Record<string, ProvidedBy> = {}
    for (const [name, entry] of Object.entries(raw)) {
      if (entry?.provided_by === "user" || entry?.provided_by === "publisher") {
        out[name] = entry.provided_by
      }
    }
    return out
  }, [agent.publish_settings])

  const inferredFor = (cred: { allow_sharing?: boolean }): ProvidedBy =>
    cred.allow_sharing ? "publisher" : "user"

  const valueFor = (cred: {
    name: string
    allow_sharing?: boolean
  }): ProvidedBy =>
    serverOverrides[cred.name] ?? inferredFor(cred)

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
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to save credential override")
    },
  })

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
    if (!bundle) return
    const key =
      mode === "conversation"
        ? "publisher_ai_credential_conversation_id"
        : "publisher_ai_credential_building_id"
    updateBundleAiMutation.mutate({ [key]: credentialId })
  }

  // ── Render ──────────────────────────────────────────────────────────

  if (!agent.is_publisher_install) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Credential provisioning</CardTitle>
        <CardDescription>
          Decide which credentials you (the publisher) provide for foreign
          installs and which the installer must supply themselves. These
          choices are baked into the next published revision.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Per-credential override map. */}
        {linkedCredentials.length === 0 ? (
          <p className="text-sm text-muted-foreground py-2">
            No service credentials linked to this install yet — link
            credentials from the Credentials tab to manage their
            provisioning here.
          </p>
        ) : (
          linkedCredentials.map((cred) => {
            const value = valueFor(cred)
            return (
              <div
                key={cred.id}
                className="flex items-start justify-between gap-4 py-2"
              >
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
                    {!cred.allow_sharing && (
                      <span className="inline-flex items-center gap-1 text-amber-700 dark:text-amber-300">
                        <AlertTriangle className="h-3 w-3 shrink-0" />
                        not shareable — enable sharing on the credential to
                        allow "Embedded (shared)"
                      </span>
                    )}
                  </div>
                </div>
                <Select
                  value={value}
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
                    <SelectItem
                      value="publisher"
                      disabled={!cred.allow_sharing}
                    >
                      Embedded (shared)
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )
          })
        )}

        {/* AI credentials. */}
        <div className="pt-2">
          <Label className="text-sm font-medium">AI credentials</Label>
          <p className="text-xs text-muted-foreground">
            Pick an AI credential to share with foreign installs, or leave
            "None — user provides" so the installer supplies their own. Only
            credentials compatible with the mode's SDK are listed.
          </p>
        </div>
        {!bundle ? (
          <p className="text-sm text-muted-foreground italic py-2">
            Publish the bundle first to manage AI credential provisioning.
          </p>
        ) : (
          <>
            {/* Conversation mode */}
            <div className="flex items-start justify-between gap-4 py-2">
              <div className="min-w-0 flex items-start gap-2">
                <MessageCircle className="h-4 w-4 text-blue-500 shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <Label className="text-sm font-medium">Conversation AI</Label>
                  <p className="text-xs text-muted-foreground">
                    SDK in use: {getEngineLabel(conversationEngine)}
                  </p>
                </div>
              </div>
              <Select
                value={
                  bundle.publisher_ai_credential_conversation_id ?? "__none__"
                }
                onValueChange={(val) =>
                  handlePickAi(
                    "conversation",
                    val === "__none__" ? null : val,
                  )
                }
                disabled={updateBundleAiMutation.isPending}
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
                <Hammer className="h-4 w-4 text-orange-500 shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <Label className="text-sm font-medium">Building AI</Label>
                  <p className="text-xs text-muted-foreground">
                    SDK in use: {getEngineLabel(buildingEngine)}
                  </p>
                </div>
              </div>
              <Select
                value={
                  bundle.publisher_ai_credential_building_id ?? "__none__"
                }
                onValueChange={(val) =>
                  handlePickAi(
                    "building",
                    val === "__none__" ? null : val,
                  )
                }
                disabled={updateBundleAiMutation.isPending}
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
        )}
      </CardContent>
    </Card>
  )
}
