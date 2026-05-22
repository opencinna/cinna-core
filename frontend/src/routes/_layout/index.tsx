import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useState, useMemo, Fragment, KeyboardEvent, DragEvent } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useSearch } from "@tanstack/react-router"

import { AgentsService, SessionsService, FilesService, UsersService, UtilsService, AiCredentialsService } from "@/client"
import type { SessionCreate, FileUploadPublic } from "@/client"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Send, Bot, Paperclip, Plus, Sparkles, Settings, AlertCircle } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  EnvironmentConfigForm,
  EnvConfigValue,
  INITIAL_ENV_CONFIG,
  USE_DEFAULT_SENTINEL,
  composeSDKId,
} from "@/components/Environments/EnvironmentConfigForm"
import { usePageHeader } from "@/routes/_layout"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import useRole from "@/hooks/useRole"
import PendingItems from "@/components/Pending/PendingItems"
import { getColorPreset } from "@/utils/colorPresets"
import { RotatingHints } from "@/components/Common/RotatingHints"
import { LatestSessions } from "@/components/Sessions/LatestSessions"
import { FileUploadModal } from "@/components/Chat/FileUploadModal"
import { FileBadge } from "@/components/Chat/FileBadge"
import { ApiKeyOnboarding } from "@/components/Onboarding/ApiKeyOnboarding"
import { GettingStartedModal } from "@/components/Onboarding/GettingStartedModal"
import { DashboardHeader } from "@/components/Dashboard/DashboardHeader"
import { EnableTwoFactorBanner } from "@/components/UserSettings/Security/EnableTwoFactorBanner"
import { APP_NAME } from "@/utils"

type DashboardSearch = {
  selectAgentId?: string
}

export const Route = createFileRoute("/_layout/")({
  component: Dashboard,
  // Optional one-shot hint used by the install flow to pre-select the
  // freshly-installed agent in the pill picker after redirecting here.
  // The component reads it once on mount, applies it, then strips it
  // from the URL so a refresh doesn't keep re-applying the same selection.
  validateSearch: (search: Record<string, unknown>): DashboardSearch => {
    const raw = search.selectAgentId
    return typeof raw === "string" && raw.length > 0
      ? { selectAgentId: raw }
      : {}
  },
  head: () => ({
    meta: [
      {
        title: `Dashboard - ${APP_NAME}`,
      },
    ],
  }),
})

const NEW_AGENT_ID = "__new_agent__"
const MAX_MESSAGE_LENGTH = 8000

function Dashboard() {
  const { setHeaderContent } = usePageHeader()
  const [selectedAgentId, setSelectedAgentId] = useState<string>("")
  const [mode, setMode] = useState<"conversation" | "building">("conversation")
  const [message, setMessage] = useState("")
  const [inputMode, setInputMode] = useState<"automatic" | "manual">("automatic")
  const [previousMode, setPreviousMode] = useState<"conversation" | "building">("conversation")
  const [attachedFiles, setAttachedFiles] = useState<FileUploadPublic[]>([])
  const [showFileModal, setShowFileModal] = useState(false)
  const [isDraggingOver, setIsDraggingOver] = useState(false)
  const [showGettingStarted, setShowGettingStarted] = useState(false)
  const [isHoveringInput, setIsHoveringInput] = useState(false)
  const [envConfigOpen, setEnvConfigOpen] = useState(false)
  const [envConfig, setEnvConfig] = useState<EnvConfigValue>(INITIAL_ENV_CONFIG)
  // Tracks whether the user explicitly picked an env template in the cog dialog.
  // When false, we omit `envName` from the navigation search params so the
  // backend's `settings.DEFAULT_AGENT_ENV_NAME` wins via the existing fallback
  // in `AgentService.create_agent_flow` — rather than coincidentally pinning
  // the frontend literal "python-env-advanced" from `INITIAL_ENV_CONFIG`.
  const [envNameTouched, setEnvNameTouched] = useState(false)
  const [inputError, setInputError] = useState<string | null>(null)

  const queryClient = useQueryClient()
  const navigate = useNavigate()
  // `selectAgentId` is a one-shot prefill hint set by the catalog install
  // flow when the new agent is already chat-ready. We apply it once below.
  const { selectAgentId } = useSearch({ from: "/_layout/" })
  const { showErrorToast } = useCustomToast()
  const { activeWorkspaceId, workspaceFilter } = useWorkspace()
  const { isAgentUser } = useRole()

  // Check if user has AI credentials configured
  const {
    data: credentialsStatus,
    isLoading: credentialsLoading,
  } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
  })

  const hasAnthropicKey = credentialsStatus?.has_anthropic_api_key ?? false

  // Fetch user's AI credentials list — used by handleSend to resolve the
  // selected credential objects when composing the SDK ID.
  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
  })

  const {
    data: agentsData,
    isLoading: agentsLoading,
  } = useQuery({
    queryKey: ["agents", workspaceFilter],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey as [string, string | undefined]
      return AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId,
      })
    },
  })

  // Phase 2 — pending shares are gone (replaced by Catalog). The catalog
  // surface lands as a separate route in the next frontend pass.

  const {
    data: sessionsData,
  } = useQuery({
    queryKey: ["sessions", "latest", 8, workspaceFilter],
    queryFn: ({ queryKey }) => {
      const [, , , workspaceId] = queryKey as [string, string, number, string | undefined]
      return SessionsService.listSessions({
        skip: 0,
        limit: 8,
        orderBy: "last_message_at",
        orderDesc: true,
        userWorkspaceId: workspaceId,
      })
    },
  })

  const createMutation = useMutation({
    mutationFn: (data: { sessionData: SessionCreate; initialMessage: string }) =>
      SessionsService.createSession({ requestBody: data.sessionData }),
    onSuccess: (session, variables) => {
      const initialMessage = variables.initialMessage
      setMessage("")
      setAttachedFiles([])
      navigate({
        to: "/session/$sessionId",
        params: { sessionId: session.id },
        search: {
          initialMessage,
          fileIds: attachedFiles.map(f => f.id).join(','),
          // Pass full file objects for optimistic display (files appear immediately in chat)
          fileObjects: attachedFiles.length > 0 ? JSON.stringify(attachedFiles) : undefined,
        } as any,
      })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create session")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["sessions"] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (fileId: string) => FilesService.deleteFile({ fileId }),
  })

  const uploadMutation = useMutation({
    mutationFn: (file: File) => {
      return FilesService.uploadFile({ formData: { file } })
    },
    onSuccess: (data) => {
      setAttachedFiles(prev => [...prev, data])
    },
  })

  const refineMutation = useMutation({
    mutationFn: () =>
      UtilsService.refinePrompt({
        requestBody: {
          user_input: message,
          has_files_attached: attachedFiles.length > 0,
          agent_id: selectedAgentId && selectedAgentId !== NEW_AGENT_ID ? selectedAgentId : null,
          mode: mode,
          is_new_agent: selectedAgentId === NEW_AGENT_ID,
        },
      }),
    onSuccess: (data) => {
      if (data.success && data.refined_prompt) {
        setMessage(data.refined_prompt)
        setInputMode("manual")
      } else if (data.error) {
        showErrorToast(data.error)
      }
    },
    onError: (error: Error) => {
      showErrorToast(error.message || "Failed to refine prompt")
    },
  })

  const agents = useMemo(() => agentsData?.data || [], [agentsData?.data])
  const agentsWithActiveEnv = useMemo(
    () => agents.filter((a) => a.active_environment_id && (a.show_on_dashboard || a.bundle_uuid)),
    [agents]
  )

  const sortedAgents = useMemo(() => {
    const ga = agentsWithActiveEnv.filter((a) => a.is_general_assistant)
    const regular = agentsWithActiveEnv.filter((a) => !a.is_general_assistant)
    return [...ga, ...regular]
  }, [agentsWithActiveEnv])

  useEffect(() => {
    setHeaderContent(<DashboardHeader />)
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // Reset selection when workspace changes
  useEffect(() => {
    setSelectedAgentId("")
    setInputMode("automatic")
  }, [activeWorkspaceId])

  useEffect(() => {
    // Don't make selection decisions while agents are still loading
    if (agentsLoading) return

    // One-shot prefill from the catalog install flow: an install that
    // came back gate-ready redirects here with ?selectAgentId=<id>. Pin
    // the picker to that agent if/when it shows up in the active list,
    // then strip the param so a manual refresh doesn't keep overriding
    // the user's later picks.
    if (selectAgentId) {
      const match = agentsWithActiveEnv.find((a) => a.id === selectAgentId)
      if (match) {
        if (selectedAgentId !== selectAgentId) {
          setSelectedAgentId(selectAgentId)
          setMode("conversation")
        }
        navigate({ to: "/", search: {}, replace: true })
        return
      }
      // Agents query may not yet contain the new install (race against
      // the post-install refetch). Hold off on the default-selection
      // branch below so we don't snap to a stale agent before the
      // freshly-installed one arrives in the list.
      return
    }

    if (!selectedAgentId) {
      if (agentsWithActiveEnv.length > 0) {
        // When agents with active environments exist, select the first one in conversation mode
        setSelectedAgentId(agentsWithActiveEnv[0].id)
        setMode("conversation")
      } else if (agents.length === 0 && !isAgentUser) {
        // When no agents exist at all, default to "New Agent" mode in building mode
        setSelectedAgentId(NEW_AGENT_ID)
        setMode("building")
      }
      // If agents exist but none have active environments, don't auto-select anything
    }
  }, [agentsWithActiveEnv, agents.length, selectedAgentId, agentsLoading, isAgentUser, selectAgentId, navigate])

  // Auto-insert entrypoint prompt when agent changes (only in automatic mode)
  useEffect(() => {
    if (!selectedAgentId || inputMode !== "automatic") return

    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)
    if (selectedAgent?.entrypoint_prompt) {
      setMessage(selectedAgent.entrypoint_prompt)
    } else {
      setMessage("")
    }
    // Clear any validation errors when auto-populating
    setInputError(null)
  }, [selectedAgentId, agentsWithActiveEnv, inputMode])

  // Force building mode when GA agent is selected
  useEffect(() => {
    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)
    if (selectedAgent?.is_general_assistant && mode !== "building") {
      setMode("building")
    }
  }, [selectedAgentId, agentsWithActiveEnv])

  // Handle input text when switching between conversation and building modes
  useEffect(() => {
    // Only apply this logic in automatic mode with a regular agent selected
    if (inputMode !== "automatic" || !selectedAgentId || selectedAgentId === NEW_AGENT_ID) {
      return
    }

    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)

    if (mode === "building") {
      // Clear input when switching to building mode (entrypoint prompt doesn't make sense)
      setMessage("")
    } else if (mode === "conversation") {
      // Restore entrypoint prompt when switching back to conversation mode
      if (selectedAgent?.entrypoint_prompt) {
        setMessage(selectedAgent.entrypoint_prompt)
      } else {
        setMessage("")
      }
    }
  }, [mode, inputMode, selectedAgentId, agentsWithActiveEnv])

  const handleSend = () => {
    const trimmedMessage = message.trim()
    if ((!trimmedMessage && attachedFiles.length === 0) || createMutation.isPending) {
      return
    }

    // Validate message length
    if (trimmedMessage.length > MAX_MESSAGE_LENGTH) {
      setInputError(`Message exceeds maximum length of ${MAX_MESSAGE_LENGTH.toLocaleString()} characters (${trimmedMessage.length.toLocaleString()} / ${MAX_MESSAGE_LENGTH.toLocaleString()})`)
      return
    }

    if (!selectedAgentId) {
      showErrorToast("Please select an agent")
      return
    }

    // Handle "New Agent" flow
    if (selectedAgentId === NEW_AGENT_ID) {
      setInputError(null)

      // Compose SDK IDs from the env config form state — same logic as
      // AddEnvironment.tsx so dashboard and agent-tab produce identical payloads.
      const allCredentials = aiCredentials?.data ?? []
      const convIsDefault = envConfig.conversationCredentialId === USE_DEFAULT_SENTINEL
      const buildIsDefault = envConfig.buildingCredentialId === USE_DEFAULT_SENTINEL
      const selConv = allCredentials.find((c) => c.id === envConfig.conversationCredentialId) ?? null
      const selBuild = allCredentials.find((c) => c.id === envConfig.buildingCredentialId) ?? null
      const sdkConversation = composeSDKId(envConfig.sdkEngineConversation, convIsDefault ? null : selConv)
      const sdkBuilding = composeSDKId(envConfig.sdkEngineBuilding, buildIsDefault ? null : selBuild)
      const useDefaultForAll = convIsDefault && buildIsDefault

      const trimmedConvModel = envConfig.modelOverrideConversation.trim()
      const trimmedBuildModel = envConfig.modelOverrideBuilding.trim()

      navigate({
        to: "/agent/creating",
        search: {
          description: trimmedMessage,
          mode,
          sdkConversation,
          sdkBuilding,
          // Only forward envName if the user explicitly chose a template.
          // Otherwise let the backend's DEFAULT_AGENT_ENV_NAME take over.
          envName: envNameTouched ? envConfig.envName : undefined,
          modelOverrideConversation: trimmedConvModel || undefined,
          modelOverrideBuilding: trimmedBuildModel || undefined,
          useDefaultAiCredentials: useDefaultForAll,
          conversationAiCredentialId:
            useDefaultForAll || convIsDefault ? undefined : envConfig.conversationCredentialId,
          buildingAiCredentialId:
            useDefaultForAll || buildIsDefault ? undefined : envConfig.buildingCredentialId,
          fileIds: attachedFiles.length > 0 ? attachedFiles.map(f => f.id).join(',') : undefined,
          fileObjects: attachedFiles.length > 0 ? JSON.stringify(attachedFiles) : undefined,
        },
      })
      // Clear attached files after navigation (they'll be handled by the session)
      setAttachedFiles([])
      return
    }

    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)
    if (!selectedAgent?.active_environment_id) {
      showErrorToast("Please start an environment for this agent first")
      return
    }

    createMutation.mutate({
      sessionData: {
        agent_id: selectedAgentId,
        mode,
        title: null,
      },
      initialMessage: trimmedMessage,
    })

    // Reset to automatic mode after sending
    setInputMode("automatic")
  }

  const handleFileUploaded = (file: FileUploadPublic) => {
    setAttachedFiles(prev => [...prev, file])
  }

  const handleFileRemove = async (fileId: string) => {
    // Optimistic update
    setAttachedFiles(prev => prev.filter(f => f.id !== fileId))
    // Call API to delete
    await deleteMutation.mutateAsync(fileId)
  }

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(true)
  }

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(false)
  }

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(false)

    const files = Array.from(e.dataTransfer.files)
    files.forEach(file => {
      // Validate file size (100MB)
      if (file.size > 100 * 1024 * 1024) {
        showErrorToast(`File ${file.name} is too large (max 100MB)`)
        return
      }
      uploadMutation.mutate(file)
    })
  }

  const handleMessageChange = (value: string) => {
    setMessage(value)
    // Switch to manual mode when user edits the input
    if (inputMode === "automatic") {
      setInputMode("manual")
    }
    // Clear error when message is within limit
    if (value.length <= MAX_MESSAGE_LENGTH) {
      setInputError(null)
    } else {
      setInputError(`Message exceeds maximum length of ${MAX_MESSAGE_LENGTH.toLocaleString()} characters`)
    }
  }

  const handleAgentClick = (agentId: string) => {
    if (agentId === NEW_AGENT_ID) {
      // New Agent selected - save current mode and switch to building
      if (selectedAgentId !== NEW_AGENT_ID) {
        setPreviousMode(mode)
      }
      setSelectedAgentId(NEW_AGENT_ID)
      setInputMode("automatic")
      setMessage("")
      setInputError(null)
      setMode("building")
      // Reset env-template "touched" flag so a fresh "+ New Agent" flow
      // defers to the backend default until the user picks a template.
      setEnvNameTouched(false)
      return
    }

    // Switching from "New Agent" to regular agent - restore previous mode and close env config dialog
    if (selectedAgentId === NEW_AGENT_ID) {
      setMode(previousMode)
      setEnvConfigOpen(false)
    }

    if (selectedAgentId === agentId && inputMode === "automatic") {
      // Agent already selected, toggle between empty and entrypoint prompt
      const agent = agentsWithActiveEnv.find((a) => a.id === agentId)
      if (message.trim()) {
        // Clear the input
        setMessage("")
      } else if (agent?.entrypoint_prompt) {
        // Insert entrypoint prompt
        setMessage(agent.entrypoint_prompt)
      }
    } else {
      // Select the agent (which will trigger entrypoint insertion via useEffect)
      setSelectedAgentId(agentId)
      setInputMode("automatic")
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  if (agentsLoading || credentialsLoading) {
    return <PendingItems />
  }

  // Show onboarding if user doesn't have Anthropic API key and hasn't skipped
  const onboardingSkipped = localStorage.getItem("onboardingSkipped") === "true"
  if (!hasAnthropicKey && !onboardingSkipped) {
    return (
      <ApiKeyOnboarding
        onComplete={() => {
          queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
          setShowGettingStarted(true)
        }}
        onSkip={() => {
          queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
        }}
      />
    )
  }

  if (agents.length > 0 && agentsWithActiveEnv.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-[calc(100vh-4rem)]">
        <div className="flex flex-col items-center justify-center text-center max-w-md">
          <div className="rounded-full bg-muted p-6 mb-6">
            <Bot className="h-12 w-12 text-muted-foreground" />
          </div>
          <h2 className="text-2xl font-semibold mb-2">No Active Environments</h2>
          <p className="text-muted-foreground mb-6">
            Please start an environment for your agent before you can start a conversation.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full" key={activeWorkspaceId ?? 'default'}>
      {/* Main centered content area */}
      <div className="flex-1 flex items-center justify-center p-6 overflow-auto">
        <div className="w-full max-w-3xl space-y-6">
          <EnableTwoFactorBanner />
          {/* Agent Selector Pills */}
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2 items-center">
              {sortedAgents.map((agent, index) => {
                const colorPreset = getColorPreset(agent.ui_color_preset)
                const isSelected = selectedAgentId === agent.id
                const isLastGA =
                  agent.is_general_assistant &&
                  (index === sortedAgents.length - 1 || !sortedAgents[index + 1]?.is_general_assistant)
                return (
                  <Fragment key={agent.id}>
                    <button
                      className={`
                        cursor-pointer px-4 py-2 text-sm rounded-md transition-all inline-flex items-center gap-1.5
                        ${colorPreset.badgeBg}
                        ${colorPreset.badgeText}
                        ${colorPreset.badgeHover}
                        ${isSelected ? colorPreset.badgeOutline : ""}
                      `}
                      onClick={() => handleAgentClick(agent.id)}
                    >
                      {agent.is_general_assistant && (
                        <Sparkles className="h-3.5 w-3.5" />
                      )}
                      {agent.name}
                    </button>
                    {isLastGA && sortedAgents.some((a) => !a.is_general_assistant) && (
                      <div className="h-6 w-px bg-border mx-1" />
                    )}
                  </Fragment>
                )
              })}
              {/* New Agent Badge */}
              {!isAgentUser && (
                <button
                  className={`
                    cursor-pointer px-4 py-2 text-sm rounded-md transition-all
                    bg-gradient-to-r from-blue-500 to-purple-600
                    text-white
                    hover:from-blue-600 hover:to-purple-700
                    ${selectedAgentId === NEW_AGENT_ID ? "ring-2 ring-blue-400 ring-offset-2" : ""}
                  `}
                  onClick={() => handleAgentClick(NEW_AGENT_ID)}
                >
                  + New Agent
                </button>
              )}
            </div>
          </div>

          {/* Message Input */}
          <div className="space-y-4">
            {/* Textarea with drag-drop support */}
            <div
              className="relative"
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              onMouseEnter={() => setIsHoveringInput(true)}
              onMouseLeave={() => setIsHoveringInput(false)}
            >
              <Textarea
                value={message}
                onChange={(e) => handleMessageChange(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={
                  selectedAgentId === NEW_AGENT_ID
                    ? "Describe what you want the agent to do and what result you expect..."
                    : mode === "building"
                      ? "Describe what you want the agent to do and what result you expect..."
                      : "Type your message to start a conversation..."
                }
                className={`min-h-[120px] max-h-[300px] resize-none text-base transition-colors pr-12 ${
                  mode === "building"
                    ? "border-orange-400 bg-orange-50 dark:bg-orange-950/20 focus-visible:ring-orange-400"
                    : ""
                } ${
                  isDraggingOver ? 'border-primary border-2 bg-primary/5' : ''
                }`}
                rows={4}
                disabled={refineMutation.isPending}
                readOnly={refineMutation.isPending}
              />
              {isDraggingOver && (
                <div className="absolute inset-0 flex items-center justify-center bg-primary/10 border-2 border-primary border-dashed rounded-md pointer-events-none">
                  <p className="text-sm font-medium text-primary">Drop files to attach</p>
                </div>
              )}
              {/* Refine Prompt Button - appears on hover */}
              {message.trim() && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => refineMutation.mutate()}
                      disabled={refineMutation.isPending}
                      className={`
                        absolute bottom-3 right-3
                        p-1.5 rounded-md
                        transition-all duration-200
                        ${isHoveringInput || refineMutation.isPending ? 'opacity-100' : 'opacity-0'}
                        ${refineMutation.isPending
                          ? 'text-amber-500 cursor-wait'
                          : 'text-muted-foreground hover:text-amber-500 hover:bg-amber-500/10 cursor-pointer'}
                      `}
                    >
                      <Sparkles
                        className={`h-4 w-4 ${refineMutation.isPending ? 'animate-pulse' : ''}`}
                      />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="top">
                    <p>Refine prompt with AI</p>
                  </TooltipContent>
                </Tooltip>
              )}
            </div>

            <div className="flex items-center justify-between">
              {/* Footer: Show error, attached files, or rotating hints */}
              <div className="flex-1 min-w-0">
                {inputError ? (
                  <div className="flex items-center gap-2 text-destructive text-sm">
                    <AlertCircle className="h-4 w-4 flex-shrink-0" />
                    <span>{inputError}</span>
                  </div>
                ) : attachedFiles.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {attachedFiles.map(file => (
                      <FileBadge
                        key={file.id}
                        file={file}
                        onRemove={() => handleFileRemove(file.id)}
                      />
                    ))}
                  </div>
                ) : (
                  <RotatingHints onClick={() => setShowGettingStarted(true)} />
                )}
              </div>

              {/* Character counter - show when approaching limit */}
              {message.length > MAX_MESSAGE_LENGTH * 0.8 && (
                <span className={`text-xs mr-3 ${message.length > MAX_MESSAGE_LENGTH ? 'text-destructive font-medium' : 'text-muted-foreground'}`}>
                  {message.length.toLocaleString()} / {MAX_MESSAGE_LENGTH.toLocaleString()}
                </span>
              )}

              {/* Mode Switch or SDK Config Cog (for New Agent) */}
              <div className="flex items-center gap-3">
                {selectedAgentId === NEW_AGENT_ID ? (
                  /* Env Config Dialog for New Agent */
                  <Dialog open={envConfigOpen} onOpenChange={setEnvConfigOpen}>
                    <DialogTrigger asChild>
                      <button
                        type="button"
                        className={`
                          p-2 rounded-lg transition-all duration-200
                          ${envConfigOpen
                            ? 'bg-gradient-to-r from-blue-500 to-purple-600 text-white'
                            : 'bg-muted hover:bg-muted/80 text-muted-foreground hover:text-foreground'}
                        `}
                      >
                        <Settings className="h-5 w-5" />
                      </button>
                    </DialogTrigger>
                    <DialogContent className="sm:max-w-[540px]">
                      <DialogHeader>
                        <DialogTitle>Configure Environment</DialogTitle>
                        <DialogDescription>
                          Pre-configure the environment your new agent will be created with.
                        </DialogDescription>
                      </DialogHeader>
                      <EnvironmentConfigForm
                        value={envConfig}
                        onChange={setEnvConfig}
                        open={envConfigOpen}
                        onTemplateChange={() => setEnvNameTouched(true)}
                      />
                      <DialogFooter>
                        <Button variant="outline" onClick={() => setEnvConfigOpen(false)}>
                          Done
                        </Button>
                      </DialogFooter>
                    </DialogContent>
                  </Dialog>
                ) : (() => {
                  const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)
                  const isGASelected = selectedAgent?.is_general_assistant ?? false
                  if (isGASelected || isAgentUser) {
                    return null
                  }
                  return (
                    /* Mode Switch for regular agents */
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-muted-foreground">
                        {mode === "conversation" ? "Conversation" : "Building"}
                      </span>
                      <label className="flex cursor-pointer select-none items-center">
                        <div className="relative">
                          <input
                            type="checkbox"
                            checked={mode === "building"}
                            onChange={() => {
                              const newMode = mode === "conversation" ? "building" : "conversation"
                              setMode(newMode)
                              // Update previousMode if not on "New Agent" so it's saved for later
                              if (selectedAgentId !== NEW_AGENT_ID) {
                                setPreviousMode(newMode)
                              }
                            }}
                            className="sr-only"
                          />
                          <div
                            className={`block h-6 w-11 rounded-full transition-colors ${
                              mode === "building" ? "bg-orange-400" : "bg-gray-300 dark:bg-gray-600"
                            }`}
                          ></div>
                          <div
                            className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                              mode === "building" ? "translate-x-5" : ""
                            }`}
                          ></div>
                        </div>
                      </label>
                    </div>
                  )
                })()}

                {/* Attach File Button */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="outline"
                      size="icon"
                      className="h-9 w-9"
                    >
                      <Plus className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent>
                    <DropdownMenuItem onClick={() => setShowFileModal(true)}>
                      <Paperclip className="h-4 w-4 mr-2" />
                      Attach File
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>

                <Button
                  onClick={handleSend}
                  disabled={createMutation.isPending || (!message.trim() && attachedFiles.length === 0) || message.length > MAX_MESSAGE_LENGTH}
                  size="icon"
                  className="h-9 w-9"
                >
                  <Send className="h-4 w-4" />
                </Button>
              </div>
            </div>
          </div>

          {/* File Upload Modal */}
          <FileUploadModal
            open={showFileModal}
            onOpenChange={setShowFileModal}
            onFileUploaded={handleFileUploaded}
          />

          {/* Getting Started Modal - shown once after API key onboarding */}
          <GettingStartedModal
            open={showGettingStarted}
            onOpenChange={setShowGettingStarted}
          />
        </div>
      </div>

      {/* Latest Sessions - Sticky at bottom, growing upward */}
      {sessionsData && sessionsData.data.length > 0 && (
        <div className="px-6 py-4">
          <div className="max-h-[45vh] overflow-y-auto">
            <div className="w-full max-w-3xl mx-auto">
              <LatestSessions limit={8} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
