import { useState, useEffect, useRef } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Trash2, AlertCircle, MessageCircle, Wrench, Plus, Pencil, Star, Key, Calendar, Sparkles, Save } from "lucide-react"
import { UsersService, AiCredentialsService, AICredentialPublic, AICredentialType } from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Input } from "@/components/ui/input"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { AICredentialDialog } from "./AICredentialDialog"
import { AffectedEnvironmentsDialog } from "./AffectedEnvironmentsDialog"

// SDK Engine options
const SDK_ENGINE_OPTIONS = [
  { value: "claude-code", label: "Claude Code" },
  { value: "opencode", label: "OpenCode" },
]

// Compatibility matrix: which credential types each SDK engine supports
const SDK_CREDENTIAL_COMPATIBILITY: Record<string, string[]> = {
  "claude-code": ["anthropic", "minimax"],
  "opencode": ["anthropic", "openai", "openai_compatible", "google"],
}

// Suggested models per credential type
const SUGGESTED_MODELS: Record<string, string[]> = {
  anthropic: ["claude-opus-4", "claude-sonnet-4-5", "claude-haiku-4-5"],
  openai: ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
  google: ["gemini-2.5-pro", "gemini-2.5-flash"],
  openai_compatible: [],
  minimax: [],
}

// Default SDK full ID per engine
const DEFAULT_SDK_FOR_ENGINE: Record<string, string> = {
  "claude-code": "claude-code/anthropic",
  "opencode": "opencode/anthropic",
}

// Sentinel value for "use default credential" selection
const USE_DEFAULT_SENTINEL = "__default__"

// Type display names
const TYPE_DISPLAY_NAMES: Record<AICredentialType, string> = {
  anthropic: "Anthropic",
  minimax: "MiniMax",
  openai_compatible: "OpenAI Compatible",
  openai: "OpenAI",
  google: "Google AI",
}

function extractEngine(sdkId: string | null | undefined): string {
  if (!sdkId) return "claude-code"
  return sdkId.includes("/") ? sdkId.split("/")[0] : sdkId
}

function composeSDKId(engine: string, credentialType: string | null): string {
  if (credentialType) return `${engine}/${credentialType}`
  return DEFAULT_SDK_FOR_ENGINE[engine] ?? `${engine}/anthropic`
}

function getCompatibleCredentials(engine: string, credentials: AICredentialPublic[]): AICredentialPublic[] {
  const compatible = SDK_CREDENTIAL_COMPATIBILITY[engine] ?? []
  return credentials.filter((c) => compatible.includes(c.type))
}

function getTypeDisplayName(type: AICredentialType): string {
  return TYPE_DISPLAY_NAMES[type] || type
}

// Format expiry date and determine badge style
function getExpiryBadgeProps(expiryDate: string | null | undefined): {
  text: string
  className: string
  tooltip: string
} | null {
  if (!expiryDate) return null

  const expiry = new Date(expiryDate)
  const now = new Date()
  const daysUntilExpiry = Math.floor((expiry.getTime() - now.getTime()) / (1000 * 60 * 60 * 24))

  const formattedDate = expiry.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric'
  })

  let className = ""
  let tooltip = ""

  if (daysUntilExpiry < 0) {
    className = "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300 border-red-200 dark:border-red-800"
    tooltip = `Expired on ${formattedDate}`
  } else if (daysUntilExpiry <= 30) {
    className = "bg-orange-100 dark:bg-orange-950 text-orange-700 dark:text-orange-300 border-orange-200 dark:border-orange-800"
    tooltip = `Expires in ${daysUntilExpiry} days (${formattedDate})`
  } else if (daysUntilExpiry <= 60) {
    className = "bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-800"
    tooltip = `Expires in ${daysUntilExpiry} days (${formattedDate})`
  } else {
    className = "bg-muted text-muted-foreground border-border"
    tooltip = `Expires on ${formattedDate} (in ${daysUntilExpiry} days)`
  }

  return { text: formattedDate, className, tooltip }
}

// ============= SDK Mode Edit Dialog =============

function getEngineLabel(engine: string): string {
  return SDK_ENGINE_OPTIONS.find((o) => o.value === engine)?.label ?? engine
}

interface ModeSummary {
  engine: string
  credential: string
  model?: string
}

function buildModeSummary(
  engine: string,
  credentialId: string,
  modelOverride: string,
  credentials: AICredentialPublic[],
  resolvedDefault: AICredentialPublic | null | undefined,
): ModeSummary {
  let credential: string
  if (credentialId === USE_DEFAULT_SENTINEL) {
    credential = resolvedDefault ? `Default (${resolvedDefault.name})` : "Default"
  } else {
    const cred = credentials.find((c) => c.id === credentialId)
    credential = cred?.name ?? "Unknown"
  }

  return {
    engine: getEngineLabel(engine),
    credential,
    model: modelOverride || undefined,
  }
}

interface SDKModeEditDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  mode: "conversation" | "building"
  engine: string
  credentialId: string
  modelOverride: string
  credentials: AICredentialPublic[]
  onSave: (engine: string, credentialId: string, modelOverride: string) => void
  isSaving: boolean
}

function SDKModeEditDialog({
  open,
  onOpenChange,
  mode,
  engine: initialEngine,
  credentialId: initialCredentialId,
  modelOverride: initialModelOverride,
  credentials,
  onSave,
  isSaving,
}: SDKModeEditDialogProps) {
  const [engine, setEngine] = useState(initialEngine)
  const [credentialId, setCredentialId] = useState(initialCredentialId)
  const [modelOverride, setModelOverride] = useState(initialModelOverride)

  // Reset local state when dialog opens
  useEffect(() => {
    if (open) {
      setEngine(initialEngine)
      setCredentialId(initialCredentialId)
      setModelOverride(initialModelOverride)
    }
  }, [open, initialEngine, initialCredentialId, initialModelOverride])

  const compatible = getCompatibleCredentials(engine, credentials)
  const selectedCredential = credentials.find((c) => c.id === credentialId) ?? null
  const suggestedModels = selectedCredential
    ? (SUGGESTED_MODELS[selectedCredential.type] ?? [])
    : []

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
  }

  const isConversation = mode === "conversation"
  const datalistId = isConversation ? "conv-edit-models" : "build-edit-models"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[440px]">
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
            <Select value={engine} onValueChange={handleEngineChange} disabled={isSaving}>
              <SelectTrigger className="h-9">
                <SelectValue placeholder="Select engine" />
              </SelectTrigger>
              <SelectContent>
                {SDK_ENGINE_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Credential */}
          <div className="space-y-1.5">
            <Label className="text-sm">Credential</Label>
            <Select value={credentialId} onValueChange={setCredentialId} disabled={isSaving}>
              <SelectTrigger className="h-9">
                <SelectValue placeholder="Select credential" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={USE_DEFAULT_SENTINEL}>Use Default</SelectItem>
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
              disabled={isSaving}
            />
            {suggestedModels.length > 0 && (
              <datalist id={datalistId}>
                {suggestedModels.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isSaving}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={isSaving}>
            <Save className="h-3.5 w-3.5 mr-2" />
            {isSaving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ============= Main Component =============

export function AICredentialsSettings() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Dialog state
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingCredential, setEditingCredential] = useState<AICredentialPublic | null>(null)

  // Affected environments dialog state
  const [showAffectedDialog, setShowAffectedDialog] = useState(false)
  const [affectedCredentialId, setAffectedCredentialId] = useState<string | null>(null)
  const [affectedCredentialName, setAffectedCredentialName] = useState<string>("")

  // SDK mode edit dialog state
  const [editingMode, setEditingMode] = useState<"conversation" | "building" | null>(null)

  // SDK preferences state — conversation mode
  const [sdkEngineConversation, setSdkEngineConversation] = useState("claude-code")
  const [credentialIdConversation, setCredentialIdConversation] = useState<string>(USE_DEFAULT_SENTINEL)
  const [modelOverrideConversation, setModelOverrideConversation] = useState("")

  // SDK preferences state — building mode
  const [sdkEngineBuilding, setSdkEngineBuilding] = useState("claude-code")
  const [credentialIdBuilding, setCredentialIdBuilding] = useState<string>(USE_DEFAULT_SENTINEL)
  const [modelOverrideBuilding, setModelOverrideBuilding] = useState("")

  // Track whether we have initialized from server state
  const hasInitialized = useRef(false)

  // Get current status (for SDK preferences and has_* flags)
  const { data: status } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
  })

  // Get list of named credentials
  const { data: credentialsList, isLoading: isLoadingCredentials } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
  })

  // Resolve default credential for each mode (for summary display)
  const { data: resolvedConvDefault } = useQuery({
    queryKey: ["resolveDefaultCredential", sdkEngineConversation],
    queryFn: () => AiCredentialsService.resolveDefaultCredential({ sdkEngine: sdkEngineConversation }),
    enabled: credentialIdConversation === USE_DEFAULT_SENTINEL,
  })

  const { data: resolvedBuildDefault } = useQuery({
    queryKey: ["resolveDefaultCredential", sdkEngineBuilding],
    queryFn: () => AiCredentialsService.resolveDefaultCredential({ sdkEngine: sdkEngineBuilding }),
    enabled: credentialIdBuilding === USE_DEFAULT_SENTINEL,
  })

  // Initialize from server data on first load
  useEffect(() => {
    if (!status || hasInitialized.current) return
    hasInitialized.current = true

    setSdkEngineConversation(extractEngine(status.default_sdk_conversation))
    setSdkEngineBuilding(extractEngine(status.default_sdk_building))

    setCredentialIdConversation(
      status.default_ai_credential_conversation_id ?? USE_DEFAULT_SENTINEL
    )
    setCredentialIdBuilding(
      status.default_ai_credential_building_id ?? USE_DEFAULT_SENTINEL
    )
    setModelOverrideConversation(status.default_model_override_conversation ?? "")
    setModelOverrideBuilding(status.default_model_override_building ?? "")
  }, [status])

  // Delete credential mutation
  const deleteMutation = useMutation({
    mutationFn: (credentialId: string) =>
      AiCredentialsService.deleteAiCredential({ credentialId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("AI credential deleted successfully")
    },
    onError: () => {
      showErrorToast("Failed to delete AI credential")
    },
  })

  // Set default mutation
  const setDefaultMutation = useMutation({
    mutationFn: (data: { credentialId: string; credentialName: string }) =>
      AiCredentialsService.setAiCredentialDefault({ credentialId: data.credentialId }),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("Default credential updated")

      setAffectedCredentialId(variables.credentialId)
      setAffectedCredentialName(variables.credentialName)
      setShowAffectedDialog(true)
    },
    onError: () => {
      showErrorToast("Failed to set default credential")
    },
  })

  // Save SDK preferences mutation
  const updateSdkMutation = useMutation({
    mutationFn: (data: {
      default_sdk_conversation?: string
      default_sdk_building?: string
      default_ai_functions_sdk?: string
      default_ai_functions_credential_id?: string | null
      default_ai_credential_conversation_id?: string | null
      default_ai_credential_building_id?: string | null
      default_model_override_conversation?: string | null
      default_model_override_building?: string | null
    }) => UsersService.updateUserMe({ requestBody: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
      queryClient.invalidateQueries({ queryKey: ["resolveDefaultCredential"] })
      showSuccessToast("SDK preferences saved successfully")
    },
    onError: () => {
      showErrorToast("Failed to save SDK preferences")
    },
  })

  const credentials = credentialsList?.data || []

  // Save handler from the mode edit dialog
  const handleModeSave = (
    mode: "conversation" | "building",
    engine: string,
    credentialId: string,
    modelOverride: string,
  ) => {
    const credId = credentialId === USE_DEFAULT_SENTINEL ? null : credentialId
    const selectedCred = credentials.find((c) => c.id === credentialId) ?? null
    const sdkId = composeSDKId(engine, selectedCred?.type ?? null)

    if (mode === "conversation") {
      setSdkEngineConversation(engine)
      setCredentialIdConversation(credentialId)
      setModelOverrideConversation(modelOverride)

      updateSdkMutation.mutate({
        default_sdk_conversation: sdkId,
        default_ai_credential_conversation_id: credId,
        default_model_override_conversation: modelOverride.trim() || null,
      }, {
        onSuccess: () => {
          setEditingMode(null)
          queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
          queryClient.invalidateQueries({ queryKey: ["currentUser"] })
          queryClient.invalidateQueries({ queryKey: ["resolveDefaultCredential"] })
        },
      })
    } else {
      setSdkEngineBuilding(engine)
      setCredentialIdBuilding(credentialId)
      setModelOverrideBuilding(modelOverride)

      updateSdkMutation.mutate({
        default_sdk_building: sdkId,
        default_ai_credential_building_id: credId,
        default_model_override_building: modelOverride.trim() || null,
      }, {
        onSuccess: () => {
          setEditingMode(null)
          queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
          queryClient.invalidateQueries({ queryKey: ["currentUser"] })
          queryClient.invalidateQueries({ queryKey: ["resolveDefaultCredential"] })
        },
      })
    }
  }

  // AI Functions SDK save (still auto-save)
  const handleAiFunctionsSdkChange = (value: string) => {
    if (value === "system") {
      updateSdkMutation.mutate({
        default_ai_functions_sdk: value,
        default_ai_functions_credential_id: null,
      })
    } else {
      updateSdkMutation.mutate({ default_ai_functions_sdk: value })
    }
  }

  const handleAddCredential = () => {
    setEditingCredential(null)
    setDialogOpen(true)
  }

  const handleEditCredential = (credential: AICredentialPublic) => {
    setEditingCredential(credential)
    setDialogOpen(true)
  }

  // Build summary strings for display
  const convSummary = buildModeSummary(
    sdkEngineConversation, credentialIdConversation, modelOverrideConversation,
    credentials, resolvedConvDefault,
  )
  const buildSummary = buildModeSummary(
    sdkEngineBuilding, credentialIdBuilding, modelOverrideBuilding,
    credentials, resolvedBuildDefault,
  )

  return (
    <div className="space-y-6">
      {/* Top Row - Credentials List and SDK Preferences */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Named Credentials Card */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle>AI Credentials</CardTitle>
                <CardDescription>
                  Manage named API credentials for AI providers.
                </CardDescription>
              </div>
              <Button onClick={handleAddCredential} size="sm">
                <Plus className="h-4 w-4 mr-2" />
                Add
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {isLoadingCredentials ? (
              <p className="text-sm text-muted-foreground">Loading credentials...</p>
            ) : credentials.length === 0 ? (
              <div className="text-sm text-muted-foreground py-6 text-center">
                <Key className="h-8 w-8 mx-auto mb-2 opacity-50" />
                <p>No credentials yet</p>
                <p className="text-xs mt-1">Add your first AI credential to get started</p>
              </div>
            ) : (
              <div className="space-y-1.5">
                {credentials.map((cred) => (
                  <div
                    key={cred.id}
                    className="flex items-center justify-between px-3 py-2 border rounded-lg"
                  >
                    {/* Left: name, default badge, and expiry badge */}
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-medium text-sm truncate">{cred.name}</span>
                      {cred.is_default && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Star className="h-3.5 w-3.5 text-amber-500 shrink-0 fill-amber-500" />
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              Default credential
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
                      {(() => {
                        const expiryBadge = getExpiryBadgeProps(cred.expiry_notification_date)
                        if (!expiryBadge) return null
                        return (
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <div className={`flex items-center gap-1 px-1.5 py-0.5 rounded border text-xs shrink-0 ${expiryBadge.className}`}>
                                  <Calendar className="h-3 w-3" />
                                  <span>{expiryBadge.text}</span>
                                </div>
                              </TooltipTrigger>
                              <TooltipContent side="top" className="text-xs">
                                {expiryBadge.tooltip}
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        )
                      })()}
                    </div>
                    {/* Right: type info and actions */}
                    <div className="flex items-center gap-2 shrink-0">
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="text-xs text-muted-foreground cursor-help">
                              {getTypeDisplayName(cred.type)}
                            </span>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="text-xs">
                            {cred.type === "openai_compatible" && cred.base_url ? (
                              <div>
                                <div>{cred.base_url}</div>
                                {cred.model && <div>Model: {cred.model}</div>}
                              </div>
                            ) : (
                              getTypeDisplayName(cred.type)
                            )}
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                      {!cred.is_default && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7"
                                onClick={() => setDefaultMutation.mutate({ credentialId: cred.id, credentialName: cred.name })}
                                disabled={setDefaultMutation.isPending}
                              >
                                <Star className="h-3.5 w-3.5" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="top" className="text-xs">
                              Set as default
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        onClick={() => handleEditCredential(cred)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        onClick={() => deleteMutation.mutate(cred.id)}
                        disabled={deleteMutation.isPending}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-destructive" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Default SDK Preferences Card — compact summary */}
        <Card>
          <CardHeader>
            <CardTitle>Default SDK Preferences</CardTitle>
            <CardDescription>
              Default AI engine, credential, and model for new environments.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">

            {/* Conversation Mode — summary row */}
            <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
              <div className="flex items-start gap-3 min-w-0">
                <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-blue-500/10 shrink-0 mt-0.5">
                  <MessageCircle className="h-3.5 w-3.5 text-blue-500" />
                </div>
                <div className="min-w-0">
                  <p className="text-xs text-muted-foreground mb-0.5">Conversation</p>
                  <p className="text-sm font-medium">{convSummary.engine}</p>
                  <p className="text-xs text-muted-foreground">{convSummary.credential}</p>
                  {convSummary.model && (
                    <p className="text-xs text-muted-foreground">Model: {convSummary.model}</p>
                  )}
                </div>
              </div>
              <Button
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
                  <p className="text-sm font-medium">{buildSummary.engine}</p>
                  <p className="text-xs text-muted-foreground">{buildSummary.credential}</p>
                  {buildSummary.model && (
                    <p className="text-xs text-muted-foreground">Model: {buildSummary.model}</p>
                  )}
                </div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 shrink-0"
                onClick={() => setEditingMode("building")}
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
            </div>

            {/* AI Functions SDK - Application level */}
            <div className="pt-2 border-t space-y-3">
              <div className="flex items-center justify-between gap-4 py-2">
                <div className="flex items-center gap-3">
                  <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-purple-500/10">
                    <Sparkles className="h-3.5 w-3.5 text-purple-500" />
                  </div>
                  <div>
                    <Label htmlFor="sdk-ai-functions" className="text-sm font-medium">
                      AI Functions
                    </Label>
                    <p className="text-xs text-muted-foreground">
                      Provider for titles, schedules, suggestions, etc.
                    </p>
                  </div>
                </div>
                <Select
                  value={status?.default_ai_functions_sdk || "system"}
                  onValueChange={handleAiFunctionsSdkChange}
                  disabled={updateSdkMutation.isPending}
                >
                  <SelectTrigger id="sdk-ai-functions" className="w-[180px]">
                    <SelectValue placeholder="Select provider" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="system">System (default)</SelectItem>
                    <SelectItem value="personal:anthropic">Personal Anthropic</SelectItem>
                    <SelectItem value="personal:openai">Personal OpenAI</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {/* Credential picker for AI Functions — shown when personal provider is selected */}
              {(status?.default_ai_functions_sdk || "system") === "personal:anthropic" && (() => {
                const anthropicCreds = credentials.filter(c => c.type === "anthropic")
                const selectedCredId = status?.default_ai_functions_credential_id || "default"
                const selectedCred = anthropicCreds.find(c => c.id === selectedCredId)

                const selectedIsOAuth = selectedCred?.is_oauth_token
                const defaultCred = anthropicCreds.find(c => c.is_default)
                const defaultIsOAuth = selectedCredId === "default" && defaultCred?.is_oauth_token

                return (
                  <div className="ml-10 space-y-2">
                    <div className="flex items-center justify-between gap-4">
                      <Label htmlFor="ai-functions-credential" className="text-xs text-muted-foreground">
                        Credential
                      </Label>
                      <Select
                        value={selectedCredId}
                        onValueChange={(value) => {
                          updateSdkMutation.mutate({
                            default_ai_functions_credential_id: value === "default" ? null : value,
                          })
                        }}
                        disabled={updateSdkMutation.isPending}
                      >
                        <SelectTrigger id="ai-functions-credential" className="w-[180px]">
                          <SelectValue placeholder="Select credential" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="default">
                            Use Default{defaultCred ? ` (${defaultCred.name})` : ""}
                          </SelectItem>
                          {anthropicCreds.map((cred) => (
                            <SelectItem
                              key={cred.id}
                              value={cred.id}
                              disabled={cred.is_oauth_token}
                            >
                              {cred.name}
                              {cred.is_oauth_token && " (OAuth - incompatible)"}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>

                    {(selectedIsOAuth || defaultIsOAuth) && (
                      <Alert variant="destructive" className="py-2">
                        <AlertCircle className="h-3.5 w-3.5" />
                        <AlertDescription className="text-xs">
                          OAuth tokens cannot be used with the Anthropic API. Please select a credential with an API key (sk-ant-api*).
                        </AlertDescription>
                      </Alert>
                    )}

                    {anthropicCreds.length === 0 && (
                      <p className="text-xs text-muted-foreground">
                        No Anthropic credentials found. Add one in the credentials panel.
                      </p>
                    )}
                  </div>
                )
              })()}

              {/* Credential picker for AI Functions — shown when personal:openai is selected */}
              {(status?.default_ai_functions_sdk || "system") === "personal:openai" && (() => {
                const openaiCreds = credentials.filter(c => c.type === "openai")
                const selectedCredId = status?.default_ai_functions_credential_id || "default"
                const defaultCred = openaiCreds.find(c => c.is_default)

                return (
                  <div className="ml-10 space-y-2">
                    <div className="flex items-center justify-between gap-4">
                      <Label htmlFor="ai-functions-openai-credential" className="text-xs text-muted-foreground">
                        Credential
                      </Label>
                      <Select
                        value={selectedCredId}
                        onValueChange={(value) => {
                          updateSdkMutation.mutate({
                            default_ai_functions_credential_id: value === "default" ? null : value,
                          })
                        }}
                        disabled={updateSdkMutation.isPending}
                      >
                        <SelectTrigger id="ai-functions-openai-credential" className="w-[180px]">
                          <SelectValue placeholder="Select credential" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="default">
                            Use Default{defaultCred ? ` (${defaultCred.name})` : ""}
                          </SelectItem>
                          {openaiCreds.map((cred) => (
                            <SelectItem key={cred.id} value={cred.id}>
                              {cred.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>

                    {openaiCreds.length === 0 && (
                      <p className="text-xs text-muted-foreground">
                        No OpenAI credentials found. Add one in the credentials panel.
                      </p>
                    )}
                  </div>
                )
              })()}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* SDK Mode Edit Dialog */}
      {editingMode && (
        <SDKModeEditDialog
          open={!!editingMode}
          onOpenChange={(open) => { if (!open) setEditingMode(null) }}
          mode={editingMode}
          engine={editingMode === "conversation" ? sdkEngineConversation : sdkEngineBuilding}
          credentialId={editingMode === "conversation" ? credentialIdConversation : credentialIdBuilding}
          modelOverride={editingMode === "conversation" ? modelOverrideConversation : modelOverrideBuilding}
          credentials={credentials}
          onSave={(engine, credentialId, modelOverride) =>
            handleModeSave(editingMode, engine, credentialId, modelOverride)
          }
          isSaving={updateSdkMutation.isPending}
        />
      )}

      {/* Add/Edit Credential Dialog */}
      <AICredentialDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        credential={editingCredential}
      />

      {/* Affected Environments Dialog */}
      {affectedCredentialId && (
        <AffectedEnvironmentsDialog
          open={showAffectedDialog}
          onOpenChange={setShowAffectedDialog}
          credentialId={affectedCredentialId}
          credentialName={affectedCredentialName}
        />
      )}
    </div>
  )
}
