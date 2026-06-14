import { useState, useEffect } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Input } from "@/components/ui/input"
import { UsersService, AiCredentialsService } from "@/client"
import type { AICredentialPublic, AgentEnvironmentPublic } from "@/client"
import { Pencil, MessageCircle, Wrench, Save, Code2, Boxes } from "lucide-react"

// ============= Constants =============

// Environment template options
export const ENV_TEMPLATE_OPTIONS = [
  {
    value: "python-env-advanced",
    label: "Python",
    description: "Slim Python image. Fast to build, ideal for pure-Python work.",
    icon: Code2,
    iconClassName: "text-emerald-500",
  },
  {
    value: "general-env",
    label: "General Purpose",
    description: "Full Debian. Install system packages (ffmpeg, sqlite, etc.).",
    icon: Boxes,
    iconClassName: "text-blue-500",
  },
]

// SDK Engine options — engine only, not coupled to credential type
export const SDK_ENGINE_OPTIONS = [
  { value: "claude-code", label: "Claude Code", description: "Anthropic's CLI agent SDK" },
  { value: "opencode", label: "OpenCode", description: "Multi-provider open-source agent (75+ providers)" },
]

// Compatibility matrix: which credential types each SDK engine supports
export const SDK_CREDENTIAL_COMPATIBILITY: Record<string, string[]> = {
  "claude-code": ["anthropic"],
  "opencode": ["anthropic", "openai", "openai_compatible", "google"],
}

// Strict mapping from full SDK id (engine + provider suffix) to the single
// credential type it accepts. Source of truth on the backend lives in
// ``app/services/environments/sdk_constants.py::SDK_TO_CREDENTIAL_TYPE``;
// keep this map in sync. Used to filter the publisher AI credential
// dropdown by exact provider match (not engine-only).
export const SDK_TO_CREDENTIAL_TYPE: Record<string, string> = {
  "claude-code/anthropic": "anthropic",
  "opencode/anthropic": "anthropic",
  "opencode/openai": "openai",
  "opencode/openai_compatible": "openai_compatible",
  "opencode/google": "google",
  "opencode": "anthropic",
}

export function sdkExpectedCredentialType(sdkId: string | null | undefined): string | null {
  if (!sdkId) return null
  return SDK_TO_CREDENTIAL_TYPE[sdkId] ?? null
}

// Suggested models per credential type (for model override hints)
export const SUGGESTED_MODELS: Record<string, string[]> = {
  anthropic: ["claude-opus-4", "claude-sonnet-4-5", "claude-haiku-4-5"],
  openai: ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
  google: ["gemini-2.5-pro", "gemini-2.5-flash"],
  openai_compatible: [],
}

// Default SDK full ID per engine (for when "Default" credential is selected)
export const DEFAULT_SDK_FOR_ENGINE: Record<string, string> = {
  "claude-code": "claude-code/anthropic",
  "opencode": "opencode/anthropic",
}

// Type display names for resolved default indicator
export const TYPE_DISPLAY_NAMES: Record<string, string> = {
  anthropic: "Anthropic",
  openai_compatible: "OpenAI Compatible",
  openai: "OpenAI",
  google: "Google AI",
}

// Sentinel value for "use default credential" selection
export const USE_DEFAULT_SENTINEL = "__default__"

// ============= Helpers =============

export function composeSDKId(engine: string, credential: AICredentialPublic | null): string {
  if (credential) {
    return `${engine}/${credential.type}`
  }
  return DEFAULT_SDK_FOR_ENGINE[engine] ?? `${engine}/anthropic`
}

/**
 * Shared, API-shaped subset of an environment's per-mode config. Produced from
 * the form's {@link EnvConfigValue} and consumed by both the create payload
 * (AddEnvironment) and the dynamic reconfigure payload (EnvironmentCard).
 */
export interface EnvModeConfigFields {
  agent_sdk_conversation: string
  agent_sdk_building: string
  model_override_conversation: string | null
  model_override_building: string | null
  use_default_ai_credentials: boolean
  conversation_ai_credential_id: string | null
  building_ai_credential_id: string | null
}

/**
 * Resolve an {@link EnvConfigValue} into the API-shaped per-mode fields, applying
 * the same default/explicit credential rules used at environment creation. The
 * "use account default" sentinel collapses to a null credential id + the
 * `use_default_ai_credentials` flag.
 */
export function composeEnvModeConfigFields(
  config: EnvConfigValue,
  credentials: AICredentialPublic[],
): EnvModeConfigFields {
  const convIsDefault = config.conversationCredentialId === USE_DEFAULT_SENTINEL
  const buildIsDefault = config.buildingCredentialId === USE_DEFAULT_SENTINEL

  const selectedConversationCredential =
    credentials.find((c) => c.id === config.conversationCredentialId) ?? null
  const selectedBuildingCredential =
    credentials.find((c) => c.id === config.buildingCredentialId) ?? null

  const useDefaultForAll = convIsDefault && buildIsDefault

  return {
    agent_sdk_conversation: composeSDKId(
      config.sdkEngineConversation,
      convIsDefault ? null : selectedConversationCredential,
    ),
    agent_sdk_building: composeSDKId(
      config.sdkEngineBuilding,
      buildIsDefault ? null : selectedBuildingCredential,
    ),
    model_override_conversation: config.modelOverrideConversation.trim() || null,
    model_override_building: config.modelOverrideBuilding.trim() || null,
    use_default_ai_credentials: useDefaultForAll,
    conversation_ai_credential_id: useDefaultForAll
      ? null
      : convIsDefault
        ? null
        : config.conversationCredentialId || null,
    building_ai_credential_id: useDefaultForAll
      ? null
      : buildIsDefault
        ? null
        : config.buildingCredentialId || null,
  }
}

/**
 * Map an existing environment row back into an {@link EnvConfigValue} so the
 * shared per-mode edit dialog can be seeded with its current configuration.
 */
export function envToEnvConfig(env: AgentEnvironmentPublic): EnvConfigValue {
  return {
    envName: env.env_name,
    sdkEngineConversation: extractEngine(env.agent_sdk_conversation),
    conversationCredentialId: env.conversation_ai_credential_id ?? USE_DEFAULT_SENTINEL,
    modelOverrideConversation: env.model_override_conversation ?? "",
    sdkEngineBuilding: extractEngine(env.agent_sdk_building),
    buildingCredentialId: env.building_ai_credential_id ?? USE_DEFAULT_SENTINEL,
    modelOverrideBuilding: env.model_override_building ?? "",
  }
}

export function getCompatibleCredentials(engine: string, credentials: AICredentialPublic[]): AICredentialPublic[] {
  const compatible = SDK_CREDENTIAL_COMPATIBILITY[engine] ?? []
  return credentials.filter((c) => compatible.includes(c.type))
}

export function extractEngine(sdkId: string | null | undefined): string {
  if (!sdkId) return "claude-code"
  return sdkId.includes("/") ? sdkId.split("/")[0] : sdkId
}

export function getEngineLabel(engine: string): string {
  return SDK_ENGINE_OPTIONS.find((o) => o.value === engine)?.label ?? engine
}

// ============= Public types =============

export interface EnvConfigValue {
  envName: string
  sdkEngineConversation: string
  conversationCredentialId: string  // UUID string OR USE_DEFAULT_SENTINEL
  modelOverrideConversation: string
  sdkEngineBuilding: string
  buildingCredentialId: string      // UUID string OR USE_DEFAULT_SENTINEL
  modelOverrideBuilding: string
}

export const INITIAL_ENV_CONFIG: EnvConfigValue = {
  envName: "python-env-advanced",
  sdkEngineConversation: "claude-code",
  conversationCredentialId: USE_DEFAULT_SENTINEL,
  modelOverrideConversation: "",
  sdkEngineBuilding: "claude-code",
  buildingCredentialId: USE_DEFAULT_SENTINEL,
  modelOverrideBuilding: "",
}

// ============= SDK Mode Edit Dialog (per-mode sub-dialog) =============

interface EnvModeEditDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  mode: "conversation" | "building"
  engine: string
  credentialId: string
  modelOverride: string
  credentials: AICredentialPublic[]
  onSave: (engine: string, credentialId: string, modelOverride: string) => void
}

export function EnvModeEditDialog({
  open,
  onOpenChange,
  mode,
  engine: initialEngine,
  credentialId: initialCredentialId,
  modelOverride: initialModelOverride,
  credentials,
  onSave,
}: EnvModeEditDialogProps) {
  const [engine, setEngine] = useState(initialEngine)
  const [credentialId, setCredentialId] = useState(initialCredentialId)
  const [modelOverride, setModelOverride] = useState(initialModelOverride)

  useEffect(() => {
    if (open) {
      setEngine(initialEngine)
      setCredentialId(initialCredentialId)
      setModelOverride(initialModelOverride)
    }
  }, [open, initialEngine, initialCredentialId, initialModelOverride])

  const compatible = getCompatibleCredentials(engine, credentials)
  const selectedCredential = credentials.find((c) => c.id === credentialId) ?? null
  // Prefer the credential's discovered (per-key) models as suggestions, falling
  // back to / augmenting the static SUGGESTED_MODELS list.
  // Prefer the admin-curated available_models when present, else the per-key
  // discovered models (see admin_curated_model_list).
  const discoveredModels = selectedCredential?.discovered_models ?? []
  const offeredModels = selectedCredential?.available_models?.length
    ? selectedCredential.available_models
    : discoveredModels
  const suggestedModels = selectedCredential
    ? Array.from(
        new Set([
          ...offeredModels,
          ...(SUGGESTED_MODELS[selectedCredential.type] ?? []),
        ]),
      )
    : []
  // Inline warning: a non-empty typed override that isn't in this key's
  // discovered list (only meaningful once discovery has populated a list).
  const overrideNotDiscovered =
    modelOverride.trim().length > 0 &&
    discoveredModels.length > 0 &&
    !discoveredModels.includes(modelOverride.trim())

  // Resolve default credential for this engine
  const { data: resolvedDefault } = useQuery({
    queryKey: ["resolveDefaultCredential", engine],
    queryFn: () => AiCredentialsService.resolveDefaultCredential({ sdkEngine: engine }),
    enabled: open && credentialId === USE_DEFAULT_SENTINEL,
  })

  const handleEngineChange = (newEngine: string) => {
    setEngine(newEngine)
    setCredentialId(USE_DEFAULT_SENTINEL)
    setModelOverride("")
  }

  const handleSave = () => {
    onSave(engine, credentialId, modelOverride)
    onOpenChange(false)
  }

  const isConversation = mode === "conversation"
  const datalistId = isConversation ? "env-conv-models" : "env-build-models"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {isConversation ? (
              <MessageCircle className="h-4 w-4 text-blue-500" />
            ) : (
              <Wrench className="h-4 w-4 text-orange-500" />
            )}
            {isConversation ? "Conversation Mode" : "Building Mode"}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {/* SDK Engine */}
          <div className="space-y-1.5">
            <Label className="text-sm">SDK Engine</Label>
            <Select value={engine} onValueChange={handleEngineChange}>
              <SelectTrigger className="h-9">
                <SelectValue placeholder="Select engine" />
              </SelectTrigger>
              <SelectContent>
                {SDK_ENGINE_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    <div>
                      <span>{opt.label}</span>
                      <span className="ml-2 text-xs text-muted-foreground">{opt.description}</span>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Credential */}
          <div className="space-y-1.5">
            <Label className="text-sm">AI Credential</Label>
            <Select value={credentialId} onValueChange={setCredentialId}>
              <SelectTrigger className="h-9">
                <SelectValue placeholder="Select credential" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={USE_DEFAULT_SENTINEL}>Default (use account default)</SelectItem>
                {compatible.map((cred) => (
                  <SelectItem key={cred.id} value={cred.id}>
                    {cred.name}
                    {cred.is_default && " (default)"}
                    <span className="ml-1 text-xs text-muted-foreground">({cred.type})</span>
                  </SelectItem>
                ))}
                {compatible.length === 0 && (
                  <div className="py-2 px-2 text-xs text-muted-foreground">
                    No compatible credentials
                  </div>
                )}
              </SelectContent>
            </Select>
            {credentialId === USE_DEFAULT_SENTINEL && (
              <p className="text-xs text-muted-foreground">
                {resolvedDefault
                  ? `Resolved: "${resolvedDefault.name}" (${TYPE_DISPLAY_NAMES[resolvedDefault.type] || resolvedDefault.type})`
                  : "No matching default credential"}
              </p>
            )}
          </div>

          {/* Model Override */}
          <div className="space-y-1.5">
            <Label className="text-sm">
              Model Override <span className="text-muted-foreground text-xs">(optional)</span>
            </Label>
            <Input
              list={datalistId}
              value={modelOverride}
              onChange={(e) => setModelOverride(e.target.value)}
              placeholder={isConversation ? "e.g., claude-haiku-4-5" : "e.g., claude-opus-4"}
              className="h-9"
            />
            {suggestedModels.length > 0 && (
              <datalist id={datalistId}>
                {suggestedModels.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            )}
            {overrideNotDiscovered ? (
              <p className="text-xs text-orange-600 dark:text-orange-400">
                This model isn't in the list of models this credential can
                access. Double-check the name, or leave empty to use the default.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Leave empty to use the SDK default for this mode.
              </p>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave}>
            <Save className="h-3.5 w-3.5 mr-2" />
            Apply
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ============= Main Form Component =============

export interface EnvironmentConfigFormProps {
  value: EnvConfigValue
  onChange: (next: EnvConfigValue) => void
  /**
   * Drives the seeding-from-defaults effect; pass `open` from the parent Dialog
   * so the form re-seeds when re-opened. When the dialog is always open (no
   * dialog wrapping), pass `true`.
   */
  open: boolean
  /**
   * Optional. Fires when the user explicitly changes the env template Select.
   * Used by callers (e.g. dashboard) that want to distinguish "user picked a
   * template" from "form was seeded with the literal default" — so they can
   * omit `env_name` from API payloads when the user didn't touch it and let
   * the backend's `settings.DEFAULT_AGENT_ENV_NAME` win instead.
   */
  onTemplateChange?: (envName: string) => void
}

export function EnvironmentConfigForm({ value, onChange, open, onTemplateChange }: EnvironmentConfigFormProps) {
  // Mode edit sub-dialog state
  const [editingMode, setEditingMode] = useState<"conversation" | "building" | null>(null)

  // Internal data fetching — kept inside the shared component so consumers don't duplicate hooks
  const { data: credentialsStatus } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
  })

  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
  })

  const allCredentials = aiCredentials?.data ?? []

  // Resolve default credentials for summary display
  const { data: resolvedConvDefault } = useQuery({
    queryKey: ["resolveDefaultCredential", value.sdkEngineConversation],
    queryFn: () => AiCredentialsService.resolveDefaultCredential({ sdkEngine: value.sdkEngineConversation }),
    enabled: value.conversationCredentialId === USE_DEFAULT_SENTINEL,
  })

  const { data: resolvedBuildDefault } = useQuery({
    queryKey: ["resolveDefaultCredential", value.sdkEngineBuilding],
    queryFn: () => AiCredentialsService.resolveDefaultCredential({ sdkEngine: value.sdkEngineBuilding }),
    enabled: value.buildingCredentialId === USE_DEFAULT_SENTINEL,
  })

  // Seed effect: when `open` flips to true AND credentialsStatus is loaded, seed the form.
  // This mirrors AddEnvironment.tsx's handleOpenChange behavior — every open re-seeds.
  useEffect(() => {
    if (!open || !credentialsStatus) return

    const defaultEngineConversation = extractEngine(credentialsStatus.default_sdk_conversation) || "claude-code"
    const defaultEngineBuilding = extractEngine(credentialsStatus.default_sdk_building) || "claude-code"

    onChange({
      envName: "python-env-advanced",
      sdkEngineConversation: defaultEngineConversation,
      conversationCredentialId:
        credentialsStatus.default_ai_credential_conversation_id ?? USE_DEFAULT_SENTINEL,
      modelOverrideConversation: credentialsStatus.default_model_override_conversation ?? "",
      sdkEngineBuilding: defaultEngineBuilding,
      buildingCredentialId:
        credentialsStatus.default_ai_credential_building_id ?? USE_DEFAULT_SENTINEL,
      modelOverrideBuilding: credentialsStatus.default_model_override_building ?? "",
    })
    // We intentionally only depend on `open` and `credentialsStatus` — `onChange`
    // identity changes shouldn't re-trigger seeding, and `value` would create a
    // feedback loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, credentialsStatus])

  // Helper for summary text rendering
  const buildSummaryCredential = (
    credentialId: string,
    resolvedDefault: AICredentialPublic | null | undefined,
  ): string => {
    if (credentialId === USE_DEFAULT_SENTINEL) {
      return resolvedDefault ? `Default (${resolvedDefault.name})` : "Default"
    }
    const cred = allCredentials.find((c) => c.id === credentialId)
    return cred?.name ?? "Unknown"
  }

  const handleModeEditSave = (
    mode: "conversation" | "building",
    engine: string,
    credentialId: string,
    modelOverride: string,
  ) => {
    if (mode === "conversation") {
      onChange({
        ...value,
        sdkEngineConversation: engine,
        conversationCredentialId: credentialId,
        modelOverrideConversation: modelOverride,
      })
    } else {
      onChange({
        ...value,
        sdkEngineBuilding: engine,
        buildingCredentialId: credentialId,
        modelOverrideBuilding: modelOverride,
      })
    }
  }

  return (
    <>
      <div className="grid gap-4 py-4">
        {/* Environment Template */}
        <div className="space-y-2">
          <Label>Environment Template</Label>
          <div className="grid grid-cols-2 gap-2">
            {ENV_TEMPLATE_OPTIONS.map((option) => {
              const Icon = option.icon
              const selected = value.envName === option.value
              return (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => {
                    onChange({ ...value, envName: option.value })
                    onTemplateChange?.(option.value)
                  }}
                  className={`flex flex-col items-start gap-1.5 p-3 border rounded-lg text-left transition-colors cursor-pointer ${
                    selected
                      ? "border-primary bg-accent"
                      : "border-border hover:border-primary/50 hover:bg-accent/50"
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <Icon className={`h-4 w-4 ${option.iconClassName}`} />
                    <span className="text-sm font-medium">{option.label}</span>
                  </div>
                  <p className="text-xs text-muted-foreground leading-snug">
                    {option.description}
                  </p>
                </button>
              )
            })}
          </div>
        </div>

        {/* Conversation Mode — summary row */}
        <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
          <div className="flex items-start gap-3 min-w-0">
            <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-blue-500/10 shrink-0 mt-0.5">
              <MessageCircle className="h-3.5 w-3.5 text-blue-500" />
            </div>
            <div className="min-w-0">
              <p className="text-xs text-muted-foreground mb-0.5">Conversation</p>
              <p className="text-sm font-medium">{getEngineLabel(value.sdkEngineConversation)}</p>
              <p className="text-xs text-muted-foreground">
                {buildSummaryCredential(value.conversationCredentialId, resolvedConvDefault)}
              </p>
              {value.modelOverrideConversation && (
                <p className="text-xs text-muted-foreground">Model: {value.modelOverrideConversation}</p>
              )}
            </div>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            onClick={() => setEditingMode("conversation")}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        </div>

        {/* Building Mode — summary row */}
        <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
          <div className="flex items-start gap-3 min-w-0">
            <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-orange-500/10 shrink-0 mt-0.5">
              <Wrench className="h-3.5 w-3.5 text-orange-500" />
            </div>
            <div className="min-w-0">
              <p className="text-xs text-muted-foreground mb-0.5">Building</p>
              <p className="text-sm font-medium">{getEngineLabel(value.sdkEngineBuilding)}</p>
              <p className="text-xs text-muted-foreground">
                {buildSummaryCredential(value.buildingCredentialId, resolvedBuildDefault)}
              </p>
              {value.modelOverrideBuilding && (
                <p className="text-xs text-muted-foreground">Model: {value.modelOverrideBuilding}</p>
              )}
            </div>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            onClick={() => setEditingMode("building")}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {/* Mode edit sub-dialog */}
      {editingMode && (
        <EnvModeEditDialog
          open={!!editingMode}
          onOpenChange={(isOpen) => { if (!isOpen) setEditingMode(null) }}
          mode={editingMode}
          engine={editingMode === "conversation" ? value.sdkEngineConversation : value.sdkEngineBuilding}
          credentialId={editingMode === "conversation" ? value.conversationCredentialId : value.buildingCredentialId}
          modelOverride={editingMode === "conversation" ? value.modelOverrideConversation : value.modelOverrideBuilding}
          credentials={allCredentials}
          onSave={(engine, credentialId, modelOverride) =>
            handleModeEditSave(editingMode, engine, credentialId, modelOverride)
          }
        />
      )}
    </>
  )
}
