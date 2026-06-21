import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { EnvironmentsService, AiCredentialsService } from "@/client"
import type { AgentEnvironmentPublic, AgentEnvironmentReconfigure } from "@/client"
import { EnvironmentStatusBadge } from "./EnvironmentStatusBadge"
import { ModelHealthBadge } from "./ModelHealthBadge"
import { EnvironmentCriticalBadge } from "./EnvironmentCriticalBadge"
import { EnvironmentActionLogsModal } from "./EnvironmentActionLogsModal"
import {
  EnvModeEditDialog,
  composeEnvModeConfigFields,
  envToEnvConfig,
  type EnvConfigValue,
} from "./EnvironmentConfigForm"
import { cn } from "@/lib/utils"
import {
  Play,
  Trash2,
  RefreshCw,
  Pause,
  Loader2,
  Wrench,
  MessageCircle,
  Box,
  ScrollText,
  SquareTerminal,
  MoreVertical,
  Pencil,
} from "lucide-react"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"
import type { EnvConsoleKind } from "@/hooks/useEnvConsoleSocket"

// Helper to get template display name
const getTemplateDisplayName = (envName: string): string => {
  if (envName === "python-env-advanced") return "Python"
  if (envName === "general-env") return "General Purpose"
  return envName
}

// Helper to get SDK display name from full SDK ID or engine prefix
const getSDKDisplayName = (sdk: string | null | undefined): string => {
  // Null SDK means "use the platform default", which the backend resolves to
  // ``claude-code/anthropic`` — mirror that here so the badge never shows a
  // misleading provider name for an unconfigured mode.
  if (!sdk) return "Claude Code"
  if (sdk === "claude-code/anthropic" || sdk === "claude-code") return "Claude Code"
  if (sdk === "opencode" || sdk === "opencode/anthropic") return "OpenCode"
  // OpenCode with specific provider — show provider in parentheses
  if (sdk === "opencode/openai") return "OpenCode (OpenAI)"
  if (sdk === "opencode/openai_compatible") return "OpenCode (Custom)"
  if (sdk === "opencode/google") return "OpenCode (Google)"
  if (sdk.startsWith("opencode/")) return "OpenCode"
  // For any other engine/provider format, capitalize the engine part
  const engine = sdk.includes("/") ? sdk.split("/")[0] : sdk
  return engine.charAt(0).toUpperCase() + engine.slice(1)
}

// Helper to build SDK badge label with optional model override
const getSDKBadgeLabel = (sdk: string | null | undefined, modelOverride?: string | null): string => {
  const name = getSDKDisplayName(sdk)
  if (modelOverride) return `${name} · ${modelOverride}`
  return name
}

interface EnvironmentCardProps {
  environment: AgentEnvironmentPublic
  agentId: string
  onActivate?: () => void
  /**
   * Marks this card as the agent's primary (active) environment. Renders a
   * highlighted border and an "Active" label. Derived by the parent from
   * ``agent.active_environment_id`` (with ``is_active`` fallback) so a still-
   * building primary env is highlighted immediately.
   */
  isPrimary?: boolean
  /**
   * When true, the card hides developer-only mutation controls
   * (Activate, Suspend, Rebuild, Delete).  Used by the agent-user
   * view of the environments tab so users can see the active env
   * without being shown CRUD affordances they can't use.
   * Defaults to false.
   */
  readOnly?: boolean
  /**
   * When true the viewer owns the agent and may follow its container logs.
   * The `Logs` action is hidden otherwise (and never in `readOnly` cards).
   * The server still enforces ownership on the WS handshake.
   */
  canFollowLogs?: boolean
  /**
   * When true the viewer is the owner AND an agent-developer/superuser, so the
   * interactive terminal is offered (still only enabled when `status==running`).
   * Never set for agent-user / guest / read-only views.
   */
  canOpenTerminal?: boolean
  /**
   * Opens the shared console drawer hosted by the parent tab. The card decides
   * visibility/enablement; the parent owns the single drawer instance.
   */
  onOpenConsole?: (environmentId: string, kind: EnvConsoleKind) => void
}

export function EnvironmentCard({
  environment,
  agentId,
  onActivate,
  isPrimary = false,
  readOnly = false,
  canFollowLogs = false,
  canOpenTerminal = false,
  onOpenConsole,
}: EnvironmentCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Per-mode (conversation/building) config editor — clicking a mode badge opens
  // the shared EnvModeEditDialog; saving reconfigures the env and rebuilds it.
  const [editingMode, setEditingMode] = useState<"conversation" | "building" | null>(null)

  // Lazily-fetched env action-log modal, opened from the amber critical block.
  const [actionLogsOpen, setActionLogsOpen] = useState(false)

  const { data: aiCredentialsData } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
    enabled: !readOnly,
  })
  const credentials = aiCredentialsData?.data ?? []

  const deleteMutation = useMutation({
    mutationFn: () => EnvironmentsService.deleteEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast("Environment has been deleted")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const rebuildMutation = useMutation({
    mutationFn: () => EnvironmentsService.rebuildEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast("Environment rebuild started. This may take a few minutes.")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to rebuild environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const suspendMutation = useMutation({
    mutationFn: () => EnvironmentsService.suspendEnvironment({ id: environment.id }),
    onSuccess: () => {
      showSuccessToast("Environment suspended successfully")
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to suspend environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
  })

  const reconfigureMutation = useMutation({
    mutationFn: (requestBody: AgentEnvironmentReconfigure) =>
      EnvironmentsService.reconfigureEnvironment({ id: environment.id, requestBody }),
    onSuccess: () => {
      showSuccessToast(
        "Configuration updated. Rebuild started — this may take a few minutes."
      )
    },
    onError: (error: any) => {
      showErrorToast(
        error.body?.detail || error.message || "Failed to update configuration"
      )
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
  })

  // Apply a single mode's edit on top of the env's current config, then confirm
  // and reconfigure + rebuild. The untouched mode keeps its current values.
  const handleModeSave = (engine: string, credentialId: string, modelOverride: string) => {
    if (!editingMode) return
    const base = envToEnvConfig(environment)
    const next: EnvConfigValue =
      editingMode === "conversation"
        ? {
            ...base,
            sdkEngineConversation: engine,
            conversationCredentialId: credentialId,
            modelOverrideConversation: modelOverride,
          }
        : {
            ...base,
            sdkEngineBuilding: engine,
            buildingCredentialId: credentialId,
            modelOverrideBuilding: modelOverride,
          }
    const fields = composeEnvModeConfigFields(next, credentials)
    if (
      confirm(
        "Apply this configuration and rebuild the environment now?\n\n" +
          "The container will be rebuilt with the new settings. " +
          "Workspace data (scripts, files, credentials) is preserved."
      )
    ) {
      reconfigureMutation.mutate({ ...fields, rebuild: true })
    }
  }

  const handleDelete = () => {
    if (confirm("Delete this environment? This action cannot be undone.")) {
      deleteMutation.mutate()
    }
  }

  const handleRebuild = () => {
    if (
      confirm(
        "Rebuild this environment?\n\n" +
          "This will:\n" +
          "• Update core system files from the template\n" +
          "• Rebuild the Docker image\n" +
          "• Preserve all workspace data (scripts, files, credentials)\n\n" +
          "Continue?"
      )
    ) {
      rebuildMutation.mutate()
    }
  }

  const handleSuspend = () => {
    if (
      confirm(
        "Suspend this environment?\n\n" +
          "This will stop the container to save resources. " +
          "The environment will automatically reactivate when you send a message or open a session."
      )
    ) {
      suspendMutation.mutate()
    }
  }

  // Check if environment is in a transitional state (starting/activating)
  const isTransitioning = [
    "creating",
    "building",
    "initializing",
    "starting",
    "activating",
  ].includes(environment.status)

  const isRunning = environment.status === "running"
  const isBuilding =
    environment.status === "creating" ||
    environment.status === "building" ||
    environment.status === "rebuilding"
  // An active+running environment can be suspended; anything else can be
  // (re)started via the same footer slot.
  const isSuspendable = environment.is_active && isRunning
  // Mode badges are editable for developers when the env isn't mid-build
  // (reconfigure is also rejected server-side during a build).
  const canEditMode = !readOnly && !isBuilding
  // Console actions are owner+role gated by the parent and never shown in
  // read-only (agent-user / guest) cards. Terminal needs a running env; logs
  // are available whenever the viewer may follow them and a container exists.
  const showLogs = !readOnly && canFollowLogs && !!onOpenConsole
  const showTerminal = !readOnly && canOpenTerminal && !!onOpenConsole

  return (
    <Card
      className={cn(
        "flex flex-col gap-3 p-4 transition-colors",
        isPrimary && "border-2 border-green-500/60 shadow-sm dark:border-green-500/50"
      )}
    >
      {/* Header: name + id + status, with the context menu in the corner */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-base font-semibold" title={environment.instance_name}>
              {environment.instance_name}
            </h3>
            {isPrimary && (
              <Badge className="shrink-0 border-green-500/50 bg-green-500/15 text-green-700 dark:text-green-400" variant="outline">
                Main
              </Badge>
            )}
          </div>
          <p className="truncate text-xs text-muted-foreground" title={environment.id}>
            {environment.id}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <EnvironmentStatusBadge status={environment.status} />
          {!readOnly && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon-sm" className="text-muted-foreground">
                  <MoreVertical className="h-4 w-4" />
                  <span className="sr-only">Environment actions</span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={handleRebuild} disabled={rebuildMutation.isPending || isBuilding}>
                  <RefreshCw className={cn("h-4 w-4", rebuildMutation.isPending && "animate-spin")} />
                  Rebuild
                </DropdownMenuItem>
                {!environment.is_active && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      onClick={handleDelete}
                      disabled={deleteMutation.isPending}
                      variant="destructive"
                    >
                      <Trash2 className="h-4 w-4" />
                      Delete
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>

      {/* Meta badges — the conversation/building mode badges are clickable to
          change their SDK / credential / model override and rebuild. */}
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary" className="gap-1 text-xs">
          <Box className="h-3 w-3" />
          {getTemplateDisplayName(environment.env_name)}
        </Badge>
        {canEditMode ? (
          <button
            type="button"
            onClick={() => setEditingMode("conversation")}
            title="Change conversation SDK / credential / model and rebuild"
          >
            <Badge
              variant="outline"
              className="gap-1 text-xs cursor-pointer transition-colors hover:border-primary/60 hover:bg-accent"
            >
              <MessageCircle className="h-3 w-3" />
              {getSDKBadgeLabel(environment.agent_sdk_conversation, environment.model_override_conversation)}
              <Pencil className="h-2.5 w-2.5 opacity-60" />
            </Badge>
          </button>
        ) : (
          <Badge variant="outline" className="gap-1 text-xs">
            <MessageCircle className="h-3 w-3" />
            {getSDKBadgeLabel(environment.agent_sdk_conversation, environment.model_override_conversation)}
          </Badge>
        )}
        {canEditMode ? (
          <button
            type="button"
            onClick={() => setEditingMode("building")}
            title="Change building SDK / credential / model and rebuild"
          >
            <Badge
              variant="outline"
              className="gap-1 text-xs cursor-pointer transition-colors hover:border-primary/60 hover:bg-accent"
            >
              <Wrench className="h-3 w-3" />
              {getSDKBadgeLabel(environment.agent_sdk_building, environment.model_override_building)}
              <Pencil className="h-2.5 w-2.5 opacity-60" />
            </Badge>
          </button>
        ) : (
          <Badge variant="outline" className="gap-1 text-xs">
            <Wrench className="h-3 w-3" />
            {getSDKBadgeLabel(environment.agent_sdk_building, environment.model_override_building)}
          </Badge>
        )}
        <ModelHealthBadge
          modelHealth={environment.model_health}
          onAction={
            canEditMode
              ? () => {
                  // Open the editor for the first flagged mode so the user can
                  // edit/clear the override, then reconfigure + restart.
                  const flagged = (environment.model_health?.modes ?? []).find(
                    (m) =>
                      m.status === "retired_override" ||
                      m.status === "unknown_model",
                  )
                  const mode = flagged?.mode === "building" ? "building" : "conversation"
                  setEditingMode(mode)
                }
              : undefined
          }
        />
      </div>

      {/* Persisted critical-state surface: container running but setup failed.
          Distinct from the green status badge — see EnvironmentCriticalBadge.
          "Show details" is owner-only (the action-logs route is owner-gated),
          so read-only cards render the block informationally without it. */}
      <EnvironmentCriticalBadge
        environment={environment}
        onShowDetails={readOnly ? undefined : () => setActionLogsOpen(true)}
      />

      {environment.last_health_check && (
        <p className="text-xs text-muted-foreground">
          <span className="font-medium">Last health check:</span>{" "}
          {new Date(environment.last_health_check).toLocaleString()}
        </p>
      )}

      {/* Footer: square icon actions (start/suspend, logs, terminal) */}
      {!readOnly && (
        <div className="mt-auto flex items-center gap-2 border-t pt-3">
          <TooltipProvider>
            {isSuspendable ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="outline"
                    onClick={handleSuspend}
                    disabled={suspendMutation.isPending}
                    className="border-amber-500/50 text-amber-600 hover:bg-amber-500/10 hover:text-amber-600 dark:text-amber-400 dark:hover:text-amber-400"
                  >
                    {suspendMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Pause className="h-4 w-4" />
                    )}
                    <span className="sr-only">Suspend</span>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Suspend environment</TooltipContent>
              </Tooltip>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="outline"
                    onClick={onActivate}
                    disabled={isTransitioning}
                    className="border-green-500/50 text-green-600 hover:bg-green-500/10 hover:text-green-600 dark:text-green-400 dark:hover:text-green-400"
                  >
                    {isTransitioning ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Play className="h-4 w-4" />
                    )}
                    <span className="sr-only">Start</span>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {isTransitioning ? "Starting…" : "Start environment"}
                </TooltipContent>
              </Tooltip>
            )}

            {/* Console actions pushed to the right edge of the footer */}
            <div className="ml-auto flex items-center gap-2">
              {showLogs && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      size="icon"
                      variant="outline"
                      onClick={() => onOpenConsole?.(environment.id, "logs")}
                    >
                      <ScrollText className="h-4 w-4" />
                      <span className="sr-only">Logs</span>
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Follow container logs</TooltipContent>
                </Tooltip>
              )}

              {showTerminal && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    {/* span wrapper so the tooltip still fires while disabled */}
                    <span className="inline-flex">
                      <Button
                        size="icon"
                        variant="outline"
                        onClick={() => onOpenConsole?.(environment.id, "terminal")}
                        disabled={!isRunning}
                      >
                        <SquareTerminal className="h-4 w-4" />
                        <span className="sr-only">Terminal</span>
                      </Button>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>
                    {isRunning
                      ? "Open an interactive shell in this environment"
                      : "Start the environment to open a terminal"}
                  </TooltipContent>
                </Tooltip>
              )}
            </div>
          </TooltipProvider>
        </div>
      )}

      {/* Per-mode SDK / credential / model editor (shared with Add Environment) */}
      {editingMode && (
        <EnvModeEditDialog
          open={!!editingMode}
          onOpenChange={(isOpen) => {
            if (!isOpen) setEditingMode(null)
          }}
          mode={editingMode}
          engine={
            editingMode === "conversation"
              ? envToEnvConfig(environment).sdkEngineConversation
              : envToEnvConfig(environment).sdkEngineBuilding
          }
          credentialId={
            editingMode === "conversation"
              ? envToEnvConfig(environment).conversationCredentialId
              : envToEnvConfig(environment).buildingCredentialId
          }
          modelOverride={
            editingMode === "conversation"
              ? envToEnvConfig(environment).modelOverrideConversation
              : envToEnvConfig(environment).modelOverrideBuilding
          }
          credentials={credentials}
          onSave={handleModeSave}
        />
      )}

      {/* Action-log modal opened from the amber critical block's "Show details".
          Owner-only — the route is owner-gated, so it's not mounted in
          read-only (agent-user) cards. */}
      {!readOnly && (
        <EnvironmentActionLogsModal
          environmentId={environment.id}
          open={actionLogsOpen}
          onOpenChange={setActionLogsOpen}
        />
      )}
    </Card>
  )
}
