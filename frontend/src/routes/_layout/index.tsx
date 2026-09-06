import { createFileRoute, Link } from "@tanstack/react-router"
import { useEffect, useRef, useState, useMemo, KeyboardEvent, DragEvent } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useSearch } from "@tanstack/react-router"

import { AgentsService, SessionsService, FilesService, UsersService, UtilsService, AiCredentialsService, ServerConfigService } from "@/client"
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
import { DisclaimerModal } from "@/components/Onboarding/DisclaimerModal"
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
  // Reactive mirror of the `onboardingSkipped` localStorage flag. Reading
  // localStorage directly during render is not reactive, so clicking "Skip
  // for now" would not re-render this page (the credentials query refetches
  // identical data and React Query's tracked-props suppress the re-render).
  const [onboardingSkipped, setOnboardingSkipped] = useState(
    () => localStorage.getItem("onboardingSkipped") === "true",
  )
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
    isError: credentialsErrored,
  } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
    // Every state polls, `has_key` included. Stopping there was justified by
    // the latch below — "a later change becomes the banner" — and the two
    // cancelled each other out: the one change the latch exists to survive is
    // an administrator revoking a credential, and a query that stops polling at
    // `has_key` never observes it. A credential going away has to be visible
    // for the non-destructive half of this gate to be worth anything.
    refetchInterval: 10_000,
  })

  // The onboarding decision, as the server took it. Three states, not two:
  // `preparing` is the person whose administrator has a key being created for
  // them right now, and putting the paste-a-key wall in front of them would be
  // asking for something they are about to be given.
  //
  // Deliberately NOT `has_anthropic_api_key` combined in the browser with a
  // second query for the pending memberships. That would make this file the
  // second implementation of a policy the server already owns, and the two
  // would answer differently the first time either side changed. The server
  // decides; this renders.
  //
  // No `?? "needs_key"`. The field is required on the server's response now, so
  // `undefined` means one thing only — *the server has not answered* — and it
  // stays distinguishable instead of being flattened into a state. A browser
  // that supplies the default is the second implementation of a policy the
  // server owns, and it is wrong in the direction that costs the most: pending,
  // paused (offline) and errored all become a confident `needs_key`.
  const onboardingState = credentialsStatus?.api_key_onboarding_state

  // Whether the full-page wall may still be shown.
  //
  // **A full-page early return is a first-render decision only.** The state
  // above polls, so without this a person who was working — mid-draft, files
  // attached, an agent selected — could have the entire page replaced by the
  // paste-a-key screen ten seconds later, losing all of it. That is a data-loss
  // bug regardless of whether the state that flipped was computed correctly,
  // and the correct state does not make it rare enough to leave in: an admin
  // deleting a credential, or a minted key being revoked, both flip it.
  //
  // Latched on the first render that has a real answer: if the wall was not
  // warranted then, it is never taken again for this mount, and a later
  // `needs_key` becomes the inline banner below instead.
  //
  // **The latch condition is `credentialsStatus !== undefined`, and nothing
  // else.** It was `!credentialsLoading`, which is not the same question:
  // React Query's `isLoading` is `isPending && isFetching`, so it is false in
  // three states that are not an answer, and the latch was wrong in both
  // directions from there. Latching `false` — a `has_key` served from a
  // five-minute cache on a fresh page entry, while the refetch that says
  // `needs_key` is still in flight — leaves somebody who needs a key with only
  // the banner, on exactly the entry where there is no draft to protect and the
  // wall is what should show. Latching `true` — offline, where `fetchStatus`
  // is "paused" and `isLoading` is therefore false — walled a person who *has*
  // a key, behind a "Skip for now" that writes a permanent localStorage flag,
  // with a paused query that never resolves to undo it.
  const wallAllowedRef = useRef<boolean | null>(null)
  if (wallAllowedRef.current === null && credentialsStatus !== undefined) {
    wallAllowedRef.current = credentialsStatus.api_key_onboarding_state === "needs_key"
  }
  const showKeyWall =
    wallAllowedRef.current === true &&
    onboardingState === "needs_key" &&
    !onboardingSkipped
  // Everything the wall would have said, said without taking the page away.
  //
  // Deliberately NOT suppressed by `onboardingSkipped`. "Skip for now" is a
  // permanent flag and it dismisses the *wall* — the thing that takes the page
  // away. A nudge that a single click silences forever is not a nudge, and the
  // account it silences it for is the one that cannot run an agent. It is still
  // suppressed while the wall is up (they are the same message) and while the
  // server has not answered (`onboardingState` is undefined then, and a banner
  // about a state nobody has stated is a guess).
  const showKeyBanner =
    !showKeyWall &&
    onboardingState !== undefined &&
    onboardingState !== "has_key"

  // Server-wide disclaimer shown at login, before the Getting Started modal.
  // Dismissal is tracked purely in browser storage (no server-side per-user
  // record): "new_users" persists in localStorage; "every_login" lives in
  // sessionStorage. Both keys are versioned so admin edits re-trigger display.
  const { data: disclaimer } = useQuery({
    queryKey: ["disclaimer"],
    queryFn: () => ServerConfigService.getDisclaimer(),
  })

  // Bumped when the user acknowledges so `shouldShowDisclaimer` re-evaluates.
  const [disclaimerAckTick, setDisclaimerAckTick] = useState(0)
  // When onboarding finishes but a disclaimer must show first, defer opening
  // Getting Started until the disclaimer is acknowledged.
  const [pendingGettingStarted, setPendingGettingStarted] = useState(false)

  const disclaimerStorageKey = (() => {
    if (!disclaimer) return null
    return disclaimer.display_mode === "every_login"
      ? `disclaimer_session_v${disclaimer.version}`
      : `disclaimer_ack_v${disclaimer.version}`
  })()

  const disclaimerSeen = (() => {
    if (!disclaimer || !disclaimerStorageKey) return false
    // Touch the ack tick so this recomputes after acknowledgment.
    void disclaimerAckTick
    const store =
      disclaimer.display_mode === "every_login"
        ? window.sessionStorage
        : window.localStorage
    return store.getItem(disclaimerStorageKey) === "true"
  })()

  const shouldShowDisclaimer = Boolean(
    disclaimer?.enabled &&
      disclaimer.markdown.trim() &&
      !disclaimerSeen,
  )

  const acknowledgeDisclaimer = () => {
    if (disclaimer && disclaimerStorageKey) {
      const store =
        disclaimer.display_mode === "every_login"
          ? window.sessionStorage
          : window.localStorage
      store.setItem(disclaimerStorageKey, "true")
    }
    setDisclaimerAckTick((t) => t + 1)
    // Now that the disclaimer is resolved, open any deferred Getting Started.
    if (pendingGettingStarted) {
      setPendingGettingStarted(false)
      setShowGettingStarted(true)
    }
  }

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
    return agentsWithActiveEnv
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

  // Force conversation mode for foreign (consumer) bundle installs — those are
  // use-only and cannot be built, regardless of the viewer's role.
  useEffect(() => {
    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)
    if (
      !!selectedAgent?.bundle_uuid &&
      !selectedAgent?.is_publisher_install &&
      mode !== "conversation"
    ) {
      setMode("conversation")
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

  // The API-key gate's non-destructive half, built once and rendered by every
  // branch that can return a page.
  //
  // `preparing` never gets the full-page wall at all — asking somebody to paste
  // a key while their administrator is creating one for them is the failure the
  // state exists to prevent — and it used to render nothing whatsoever, so that
  // person saw a dashboard with no explanation of why nothing worked yet.
  // `needs_key` lands here too once the page has loaded, rather than replacing
  // it.
  //
  // A **variable, not JSX inline in the main return**, because the main return
  // is not the only page this component renders: an account whose agents all
  // have stopped environments takes the "No Active Environments" early return
  // below, and every `preparing` / `needs_key` user in that state — which is
  // most of them, since an agent with no key does not keep an environment up —
  // saw the one explanation they needed suppressed by the one branch they were
  // guaranteed to land on.
  const keyStatusBanner = credentialsErrored ? (
    <div className="flex items-start gap-3 rounded-md border bg-muted/40 px-4 py-3">
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="space-y-1 text-sm">
        {/* The one honest thing to say when the server did not answer. Silence
            here reads as "everything is fine", which is the state we
            specifically do not know we are in. */}
        <p className="font-medium">Could not check your AI credentials</p>
        <p className="text-muted-foreground">
          Agents may not be able to run. This retries on its own; if it keeps
          happening, check{" "}
          <Link className="underline" to="/settings" hash="ai-credentials">
            Settings
          </Link>
          .
        </p>
      </div>
    </div>
  ) : showKeyBanner ? (
    <div className="flex items-start gap-3 rounded-md border bg-muted/40 px-4 py-3">
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="space-y-1 text-sm">
        {onboardingState === "preparing" ? (
          <>
            <p className="font-medium">Your AI access is being set up</p>
            <p className="text-muted-foreground">
              An administrator is creating an API key for your account. This
              page updates on its own when it is ready — there is nothing for
              you to do.
            </p>
          </>
        ) : (
          <>
            <p className="font-medium">No AI credential yet</p>
            <p className="text-muted-foreground">
              Agents need an API key before they can run. Add one in{" "}
              {/* A router `Link`, not `<a href>`. The bare anchor was a full
                  document reload, which threw away the draft, the attached
                  files and the agent selection this banner exists precisely to
                  preserve — and landed on the default tab, not the one holding
                  the control it is sending them to. */}
              <Link className="underline" to="/settings" hash="ai-credentials">
                Settings
              </Link>
              , or make an existing credential your default.
            </p>
          </>
        )}
      </div>
    </div>
  ) : null

  if (agentsLoading || credentialsLoading) {
    return <PendingItems />
  }

  // The server-wide disclaimer is blocking and must be acknowledged before
  // anything else — including the API-key onboarding screen below. A brand-new
  // user has no AI credentials, so without this gate they would be sent
  // straight to ApiKeyOnboarding and never see the disclaimer (the modal was
  // only mounted in the main return, which the onboarding/no-active-env early
  // returns skip). Gate it ahead of every other early return so it always
  // appears first. Once acknowledged, this re-evaluates to false and the
  // normal flow (onboarding, then Getting Started) resumes.
  if (shouldShowDisclaimer && disclaimer) {
    return (
      <DisclaimerModal
        open
        markdown={disclaimer.markdown}
        onAcknowledge={acknowledgeDisclaimer}
      />
    )
  }

  // Show onboarding when the server says a key is needed and the user has not
  // dismissed it. `onboardingSkipped` is a browser-local dismissal, which is
  // the one fact the server does not hold. `showKeyWall` additionally requires
  // that this was already true on first render — see its definition.
  if (showKeyWall) {
    return (
      <ApiKeyOnboarding
        onComplete={() => {
          queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
          // If a disclaimer must show first, defer Getting Started until it is
          // acknowledged; otherwise open it immediately.
          if (shouldShowDisclaimer) {
            setPendingGettingStarted(true)
          } else {
            setShowGettingStarted(true)
          }
        }}
        onSkip={() => {
          setOnboardingSkipped(true)
          queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
        }}
      />
    )
  }

  if (agents.length > 0 && agentsWithActiveEnv.length === 0) {
    return (
      <div className="flex h-full flex-col">
        {keyStatusBanner && (
          <div className="px-6 pt-4">
            <div className="mx-auto w-full max-w-3xl">{keyStatusBanner}</div>
          </div>
        )}
        {/* `flex-1 min-h-0` rather than a viewport-height minimum: the banner
            above stacks on top of this block, and a near-full-viewport minimum
            would push the page into a scrollbar for what is otherwise one
            centred card. */}
        <div className="flex min-h-0 flex-1 items-center justify-center">
          <div className="flex flex-col items-center justify-center text-center max-w-md">
            <div className="rounded-full bg-muted p-6 mb-6">
              <Bot className="h-12 w-12 text-muted-foreground" />
            </div>
            <h2 className="text-2xl font-semibold mb-2">No Active Environments</h2>
            <p className="text-muted-foreground mb-6">
              Please start an environment for your agent before you can start a
              conversation.
            </p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full" key={activeWorkspaceId ?? 'default'}>
      {/* Top-anchored nudge banner — sits at the page top border instead
          of inside the vertically-centered content block so it doesn't
          overlap with the agent selector. */}
      <div className="px-6 pt-4">
        <div className="mx-auto w-full max-w-3xl space-y-3">
          <EnableTwoFactorBanner />
          {keyStatusBanner}
        </div>
      </div>
      {/* Main centered content area */}
      <div className="flex-1 flex items-center justify-center p-6 overflow-auto">
        <div className="w-full max-w-3xl space-y-6">
          {/* Agent Selector Pills */}
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2 items-center">
              {sortedAgents.map((agent) => {
                const colorPreset = getColorPreset(agent.ui_color_preset)
                const isSelected = selectedAgentId === agent.id
                return (
                  // `data-color-preset` exposes the agent's colour identity to
                  // CSS, so a skin can restyle a preset without the preset table
                  // needing to know the skin exists.
                  <button
                    key={agent.id}
                    data-color-preset={colorPreset.value}
                    className={`
                      cursor-pointer px-4 py-2 text-sm rounded-md transition-all inline-flex items-center gap-1.5
                      ${colorPreset.badgeBg}
                      ${colorPreset.badgeText}
                      ${colorPreset.badgeHover}
                      ${isSelected ? colorPreset.badgeOutline : ""}
                    `}
                    onClick={() => handleAgentClick(agent.id)}
                  >
                    {agent.name}
                  </button>
                )
              })}
              {/* New Agent Badge */}
              {!isAgentUser && (
                <button
                  data-ui="new-agent"
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
                  // Foreign (consumer) bundle installs are use-only: hide the
                  // Building-mode switch for every role, not just agent-users.
                  const isForeignSelected =
                    !!selectedAgent?.bundle_uuid && !selectedAgent?.is_publisher_install
                  if (isAgentUser || isForeignSelected) {
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

          {/* Getting Started Modal - shown once after API key onboarding.
              The disclaimer is handled by a blocking early return above, so by
              the time we render here it has already been acknowledged. */}
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
