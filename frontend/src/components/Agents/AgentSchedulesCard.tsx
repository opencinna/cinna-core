import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CalendarClock,
  Plus,
  Trash2,
  Pencil,
  Sparkles,
  Check,
  Clock,
  AlertCircle,
  MinusCircle,
  Power,
  PowerOff,
  Terminal,
  FileText,
  ChevronDown,
  ChevronUp,
  History,
  ExternalLink,
  Play,
} from "lucide-react"
import { useState } from "react"

import { AgentsService } from "@/client"
import type {
  AgentSchedulePublic,
  AgentScheduleLogPublic,
} from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Textarea } from "@/components/ui/textarea"
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
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

interface AgentSchedulesCardProps {
  agentId: string
  /**
   * When true, the schedule definitions are bundle-authored and the consumer
   * may not create/edit/delete them. The enable/disable toggle, Run now and
   * Logs actions remain available (the backend allows a PUT with only
   * `{enabled}` on foreign installs).
   */
  readOnly?: boolean
}

type CreateStep = "type_select" | "form"
type ScheduleType = "static_prompt" | "script_trigger"

function formatNextExecution(isoDate?: string | null): string {
  if (!isoDate) return "Unknown"
  try {
    let utcDateString = isoDate
    if (
      !isoDate.endsWith("Z") &&
      !isoDate.includes("+") &&
      isoDate.includes("T")
    ) {
      utcDateString = isoDate + "Z"
    }
    const date = new Date(utcDateString)
    return date.toLocaleString(undefined, {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    })
  } catch {
    return isoDate
  }
}

function formatExecutedAt(isoDate: string): string {
  try {
    let utcDateString = isoDate
    if (
      !isoDate.endsWith("Z") &&
      !isoDate.includes("+") &&
      isoDate.includes("T")
    ) {
      utcDateString = isoDate + "Z"
    }
    const date = new Date(utcDateString)
    const now = new Date()
    const diffMs = now.getTime() - date.getTime()
    const diffMinutes = Math.floor(diffMs / 60000)
    const diffHours = Math.floor(diffMinutes / 60)
    const diffDays = Math.floor(diffHours / 24)

    if (diffMinutes < 1) return "Just now"
    if (diffMinutes < 60) return `${diffMinutes}m ago`
    if (diffHours < 24) return `${diffHours}h ago`
    if (diffDays < 7) return `${diffDays}d ago`

    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })
  } catch {
    return isoDate
  }
}

function LogStatusBadge({ log }: { log: AgentScheduleLogPublic }) {
  if (log.status === "success") {
    const label =
      log.schedule_type === "script_trigger" ? "OK" : "Session created"
    return (
      <Badge
        variant="outline"
        className="text-xs text-green-600 border-green-300 bg-green-50"
      >
        <Check className="h-3 w-3 mr-1" />
        {label}
      </Badge>
    )
  }
  if (log.status === "session_triggered") {
    return (
      <Badge
        variant="outline"
        className="text-xs text-amber-600 border-amber-300 bg-amber-50"
      >
        <Terminal className="h-3 w-3 mr-1" />
        Session triggered
      </Badge>
    )
  }
  if (log.status === "skipped") {
    return (
      <Badge
        variant="outline"
        className="text-xs text-gray-600 border-gray-300 bg-gray-50"
      >
        <MinusCircle className="h-3 w-3 mr-1" />
        Skipped
      </Badge>
    )
  }
  return (
    <Badge
      variant="outline"
      className="text-xs text-red-600 border-red-300 bg-red-50"
    >
      <AlertCircle className="h-3 w-3 mr-1" />
      Error
    </Badge>
  )
}

function LogDetailRow({ log }: { log: AgentScheduleLogPublic }) {
  const [expanded, setExpanded] = useState(false)

  const hasDetail =
    log.prompt_used ||
    log.command_executed ||
    log.command_output ||
    log.error_message

  return (
    <div className="border rounded-md overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 bg-muted/30">
        <div className="flex items-center gap-2 min-w-0">
          <Badge variant="outline" className="text-xs shrink-0">
            {log.schedule_type === "script_trigger"
              ? "Script trigger"
              : "Static prompt"}
          </Badge>
          <span className="text-xs text-muted-foreground shrink-0">
            {formatExecutedAt(log.executed_at as unknown as string)}
          </span>
          <LogStatusBadge log={log} />
        </div>
        {hasDetail && (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs shrink-0"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? (
              <ChevronUp className="h-3 w-3" />
            ) : (
              <ChevronDown className="h-3 w-3" />
            )}
            {expanded ? "Hide" : "View"}
          </Button>
        )}
      </div>

      {expanded && (
        <div className="px-3 py-2 space-y-2 bg-background border-t text-xs">
          {log.prompt_used && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Prompt used
              </div>
              <div className="bg-muted rounded p-2 whitespace-pre-wrap break-words">
                {log.prompt_used}
              </div>
            </div>
          )}
          {log.command_executed && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Command
              </div>
              <code className="bg-muted rounded px-2 py-1 block font-mono break-all">
                {log.command_executed}
              </code>
            </div>
          )}
          {log.command_exit_code !== null &&
            log.command_exit_code !== undefined && (
              <div className="flex items-center gap-2">
                <span className="font-medium text-muted-foreground">
                  Exit code:
                </span>
                <code
                  className={`px-2 py-0.5 rounded font-mono ${
                    log.command_exit_code === 0
                      ? "bg-green-100 text-green-700"
                      : "bg-red-100 text-red-700"
                  }`}
                >
                  {log.command_exit_code}
                </code>
              </div>
            )}
          {log.command_output && (
            <div>
              <div className="font-medium text-muted-foreground mb-1">
                Output
              </div>
              <pre className="bg-muted rounded p-2 overflow-auto max-h-40 font-mono text-xs whitespace-pre-wrap break-words">
                {log.command_output}
              </pre>
            </div>
          )}
          {log.error_message && (
            <div>
              <div className="font-medium text-red-600 mb-1">Error</div>
              <div className="bg-red-50 border border-red-200 rounded p-2 text-red-700 break-words">
                {log.error_message}
              </div>
            </div>
          )}
          {log.session_id && (
            <div className="flex items-center gap-2">
              <span className="font-medium text-muted-foreground">
                Session:
              </span>
              <a
                href={`/session/${log.session_id}`}
                className="text-primary hover:underline flex items-center gap-1"
                target="_blank"
                rel="noopener noreferrer"
              >
                View session
                <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function AgentSchedulesCard({
  agentId,
  readOnly = false,
}: AgentSchedulesCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone

  // Create dialog state
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [createStep, setCreateStep] = useState<CreateStep>("type_select")
  const [createType, setCreateType] = useState<ScheduleType>("static_prompt")
  const [createName, setCreateName] = useState("")
  const [createInput, setCreateInput] = useState("")
  const [createPrompt, setCreatePrompt] = useState("")
  const [createCommand, setCreateCommand] = useState("")
  const [createGenerated, setCreateGenerated] = useState<{
    description: string
    cron_string: string
    next_execution: string
  } | null>(null)
  const [createError, setCreateError] = useState<string | null>(null)

  // Edit dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingSchedule, setEditingSchedule] =
    useState<AgentSchedulePublic | null>(null)
  const [editName, setEditName] = useState("")
  const [editInput, setEditInput] = useState("")
  const [editPrompt, setEditPrompt] = useState("")
  const [editCommand, setEditCommand] = useState("")
  const [editGenerated, setEditGenerated] = useState<{
    description: string
    cron_string: string
    next_execution: string
  } | null>(null)
  const [editError, setEditError] = useState<string | null>(null)

  // Logs modal state
  const [logsModalOpen, setLogsModalOpen] = useState(false)
  const [logsSchedule, setLogsSchedule] =
    useState<AgentSchedulePublic | null>(null)

  const queryKey = ["agent-schedules", agentId]

  // Fetch schedules
  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => AgentsService.listSchedules({ id: agentId }),
    enabled: !!agentId,
  })

  const schedules = data?.data ?? []

  // Fetch logs (on-demand when modal is open)
  const { data: logsData, isLoading: logsLoading } = useQuery({
    queryKey: ["schedule-logs", logsSchedule?.id],
    queryFn: () =>
      AgentsService.listScheduleLogs({
        id: agentId,
        scheduleId: logsSchedule!.id,
      }),
    enabled: logsModalOpen && !!logsSchedule,
  })

  const logs = logsData?.data ?? []

  // Generate mutation (stateless AI)
  const generateMutation = useMutation({
    mutationFn: ({
      naturalLanguage,
      scheduleType,
    }: {
      naturalLanguage: string
      scheduleType: ScheduleType
    }) =>
      AgentsService.generateSchedule({
        id: agentId,
        requestBody: {
          natural_language: naturalLanguage,
          timezone: userTimezone,
          schedule_type: scheduleType,
        },
      }),
  })

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (body: {
      name: string
      cron_string: string
      timezone: string
      description: string
      prompt?: string | null
      enabled: boolean
      schedule_type: string
      command?: string | null
    }) => AgentsService.createSchedule({ id: agentId, requestBody: body }),
    onSuccess: () => {
      showSuccessToast("Schedule created")
      queryClient.invalidateQueries({ queryKey })
      handleCreateDialogClose(false)
    },
    onError: (error: unknown) => {
      const msg =
        error instanceof Error ? error.message : "Failed to create schedule"
      showErrorToast(msg)
    },
  })

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: ({
      scheduleId,
      body,
    }: {
      scheduleId: string
      body: Record<string, unknown>
    }) =>
      AgentsService.updateSchedule({
        id: agentId,
        scheduleId,
        requestBody: body,
      }),
    onSuccess: () => {
      showSuccessToast("Schedule updated")
      queryClient.invalidateQueries({ queryKey })
      setEditDialogOpen(false)
    },
    onError: (error: unknown) => {
      const msg =
        error instanceof Error ? error.message : "Failed to update schedule"
      showErrorToast(msg)
    },
  })

  // Toggle mutation
  const toggleMutation = useMutation({
    mutationFn: ({
      scheduleId,
      enabled,
    }: {
      scheduleId: string
      enabled: boolean
    }) =>
      AgentsService.updateSchedule({
        id: agentId,
        scheduleId,
        requestBody: { enabled },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey })
    },
    onError: (error: unknown) => {
      const msg =
        error instanceof Error ? error.message : "Failed to toggle schedule"
      showErrorToast(msg)
    },
  })

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (scheduleId: string) =>
      AgentsService.deleteSchedule({ id: agentId, scheduleId }),
    onSuccess: () => {
      showSuccessToast("Schedule deleted")
      queryClient.invalidateQueries({ queryKey })
    },
    onError: (error: unknown) => {
      const msg =
        error instanceof Error ? error.message : "Failed to delete schedule"
      showErrorToast(msg)
    },
  })

  // Run now mutation
  const runNowMutation = useMutation({
    mutationFn: (scheduleId: string) =>
      AgentsService.runScheduleNow({ id: agentId, scheduleId }),
    onSuccess: (response) => {
      // The backend returns a context-aware message — synchronous executions
      // confirm success, while suspended/stopped envs return an
      // "environment is starting" message so the user knows the run was
      // deferred rather than failed.
      showSuccessToast(response.message || "Schedule triggered successfully")
      queryClient.invalidateQueries({ queryKey })
      // Also refresh the agent so the env-status indicator on the agent
      // page picks up the new `activating` state when a deferred run kicks
      // off — otherwise the user sees "starting…" toast but no visible
      // env state change.
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
    onError: (error: unknown) => {
      const msg =
        error instanceof Error ? error.message : "Failed to trigger schedule"
      showErrorToast(msg)
    },
  })

  // Create dialog handlers
  const handleCreateDialogClose = (open: boolean) => {
    setCreateDialogOpen(open)
    if (!open) {
      setCreateStep("type_select")
      setCreateType("static_prompt")
      setCreateName("")
      setCreateInput("")
      setCreatePrompt("")
      setCreateCommand("")
      setCreateGenerated(null)
      setCreateError(null)
    }
  }

  const handleTypeSelect = (type: ScheduleType) => {
    setCreateType(type)
    setCreateStep("form")
  }

  const handleCreateGenerate = async () => {
    if (!createInput.trim()) return
    setCreateError(null)
    setCreateGenerated(null)
    try {
      const result = await generateMutation.mutateAsync({
        naturalLanguage: createInput,
        scheduleType: createType,
      })
      if (result.success && result.cron_string && result.description) {
        setCreateGenerated({
          description: result.description,
          cron_string: result.cron_string,
          next_execution: result.next_execution ?? "",
        })
      } else {
        setCreateError(result.error || "Failed to generate schedule")
      }
    } catch (error: unknown) {
      const msg =
        error instanceof Error ? error.message : "Failed to generate schedule"
      setCreateError(msg)
    }
  }

  const handleCreate = () => {
    if (!createName.trim() || !createGenerated) return
    if (createType === "script_trigger" && !createCommand.trim()) return

    createMutation.mutate({
      name: createName,
      cron_string: createGenerated.cron_string,
      timezone: userTimezone,
      description: createGenerated.description,
      prompt: createType === "static_prompt" ? createPrompt.trim() || null : null,
      enabled: true,
      schedule_type: createType,
      command: createType === "script_trigger" ? createCommand.trim() : null,
    })
  }

  // Edit dialog handlers
  const handleEditOpen = (schedule: AgentSchedulePublic) => {
    setEditingSchedule(schedule)
    setEditName(schedule.name)
    setEditInput("")
    setEditPrompt(schedule.prompt ?? "")
    setEditCommand(schedule.command ?? "")
    setEditGenerated(null)
    setEditError(null)
    setEditDialogOpen(true)
  }

  const handleEditGenerate = async () => {
    if (!editInput.trim() || !editingSchedule) return
    setEditError(null)
    setEditGenerated(null)
    try {
      const result = await generateMutation.mutateAsync({
        naturalLanguage: editInput,
        scheduleType: editingSchedule.schedule_type as ScheduleType,
      })
      if (result.success && result.cron_string && result.description) {
        setEditGenerated({
          description: result.description,
          cron_string: result.cron_string,
          next_execution: result.next_execution ?? "",
        })
      } else {
        setEditError(result.error || "Failed to generate schedule")
      }
    } catch (error: unknown) {
      const msg =
        error instanceof Error ? error.message : "Failed to generate schedule"
      setEditError(msg)
    }
  }

  const handleEditSave = () => {
    if (!editingSchedule) return
    const body: Record<string, unknown> = {}

    if (editName !== editingSchedule.name) body.name = editName

    if (editingSchedule.schedule_type === "static_prompt") {
      if (editPrompt !== (editingSchedule.prompt ?? "")) {
        body.prompt = editPrompt.trim() || null
      }
    } else if (editingSchedule.schedule_type === "script_trigger") {
      if (editCommand !== (editingSchedule.command ?? "")) {
        body.command = editCommand.trim() || null
      }
    }

    if (editGenerated) {
      body.cron_string = editGenerated.cron_string
      body.timezone = userTimezone
      body.description = editGenerated.description
    }

    updateMutation.mutate({ scheduleId: editingSchedule.id, body })
  }

  const handleShowLogs = (schedule: AgentSchedulePublic) => {
    setLogsSchedule(schedule)
    setLogsModalOpen(true)
  }

  const isCreateDisabled =
    !createName.trim() ||
    !createGenerated ||
    createMutation.isPending ||
    (createType === "script_trigger" && !createCommand.trim())

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <CalendarClock className="h-5 w-5" />
              Schedules
            </CardTitle>
            <CardDescription>
              Schedule execution times for this agent with different prompts and cadences
            </CardDescription>
          </div>
          {!readOnly && (
          <Dialog open={createDialogOpen} onOpenChange={handleCreateDialogClose}>
            <DialogTrigger asChild>
              <Button size="sm">
                <Plus className="h-4 w-4 mr-1" />
                New
              </Button>
            </DialogTrigger>
            <DialogContent>
              {createStep === "type_select" ? (
                <>
                  <DialogHeader>
                    <DialogTitle>Create Schedule</DialogTitle>
                    <DialogDescription>
                      Choose the type of schedule to create.
                    </DialogDescription>
                  </DialogHeader>
                  <div className="grid grid-cols-2 gap-3 py-2">
                    {/* Static Prompt card */}
                    <button
                      onClick={() => handleTypeSelect("static_prompt")}
                      className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
                    >
                      <div className="flex items-center gap-2">
                        <FileText className="h-5 w-5 text-primary" />
                        <span className="font-medium text-sm">
                          Static Prompt
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground leading-relaxed">
                        Best for agentic workflows that always need a
                        conversational starting point. A session is created
                        every run, so each execution consumes tokens.
                      </p>
                    </button>

                    {/* Script Trigger card */}
                    <button
                      onClick={() => handleTypeSelect("script_trigger")}
                      className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
                    >
                      <div className="flex items-center gap-2">
                        <Terminal className="h-5 w-5 text-amber-600" />
                        <span className="font-medium text-sm">
                          Script Trigger
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground leading-relaxed">
                        Best for direct scenarios covered by a predefined
                        script. The agent is only involved when something
                        needs attention, so if everything runs smoothly it
                        may not consume any tokens at all.
                      </p>
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <DialogHeader>
                    <DialogTitle>
                      {createType === "script_trigger"
                        ? "Create Script Trigger Schedule"
                        : "Create Static Prompt Schedule"}
                    </DialogTitle>
                    <DialogDescription>
                      <button
                        onClick={() => setCreateStep("type_select")}
                        className="text-primary hover:underline text-sm"
                      >
                        &larr; Back
                      </button>
                    </DialogDescription>
                  </DialogHeader>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="schedule-name">Name</Label>
                      <Input
                        id="schedule-name"
                        placeholder="e.g., Daily health check"
                        value={createName}
                        onChange={(e) => setCreateName(e.target.value)}
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Timing</Label>
                      <div className="flex gap-2">
                        <Input
                          value={createInput}
                          onChange={(e) => setCreateInput(e.target.value)}
                          onKeyDown={(e) =>
                            e.key === "Enter" && handleCreateGenerate()
                          }
                          placeholder="e.g., every workday at 7am"
                          disabled={generateMutation.isPending}
                        />
                        <Button
                          onClick={handleCreateGenerate}
                          disabled={
                            !createInput.trim() || generateMutation.isPending
                          }
                          variant="outline"
                        >
                          {generateMutation.isPending ? (
                            "..."
                          ) : (
                            <>
                              <Sparkles className="h-4 w-4 mr-1" />
                              Generate
                            </>
                          )}
                        </Button>
                      </div>
                      {createGenerated && (
                        <div className="bg-secondary p-3 rounded-md space-y-1.5">
                          <div className="flex items-start gap-2">
                            <Check className="h-4 w-4 text-green-600 mt-0.5 shrink-0" />
                            <span className="font-medium text-sm">
                              {createGenerated.description}
                            </span>
                          </div>
                          {createGenerated.next_execution && (
                            <div className="flex items-start gap-2 text-sm text-muted-foreground">
                              <Clock className="h-4 w-4 mt-0.5 shrink-0" />
                              <span>
                                Next:{" "}
                                {formatNextExecution(
                                  createGenerated.next_execution
                                )}
                              </span>
                            </div>
                          )}
                        </div>
                      )}
                      {createError && (
                        <div className="bg-destructive/10 border border-destructive/20 p-3 rounded-md">
                          <div className="flex items-start gap-2">
                            <AlertCircle className="h-4 w-4 text-destructive mt-0.5 shrink-0" />
                            <span className="text-sm text-destructive">
                              {createError}
                            </span>
                          </div>
                        </div>
                      )}
                    </div>

                    {createType === "static_prompt" ? (
                      <div className="space-y-2">
                        <Label htmlFor="schedule-prompt">
                          Prompt (optional)
                        </Label>
                        <Textarea
                          id="schedule-prompt"
                          placeholder="Leave empty to use agent's entrypoint prompt"
                          value={createPrompt}
                          onChange={(e) => setCreatePrompt(e.target.value)}
                          rows={3}
                        />
                      </div>
                    ) : (
                      <div className="space-y-2">
                        <Label htmlFor="schedule-command">Command</Label>
                        <Input
                          id="schedule-command"
                          placeholder="e.g., bash scripts/check_status.sh"
                          value={createCommand}
                          onChange={(e) => setCreateCommand(e.target.value)}
                          maxLength={2000}
                          className="font-mono text-sm"
                        />
                        <p className="text-xs text-muted-foreground">
                          Command to execute inside the agent environment. If it
                          returns &quot;OK&quot;, no session is started. Any
                          other output triggers a new agent session with the
                          execution context.
                        </p>
                      </div>
                    )}
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={handleCreate}
                      disabled={isCreateDisabled}
                    >
                      {createMutation.isPending ? "Creating..." : "Create"}
                    </Button>
                  </DialogFooter>
                </>
              )}
            </DialogContent>
          </Dialog>
          )}
        </div>
        {readOnly && (
          <p className="text-xs text-muted-foreground">
            Managed by the bundle publisher — you can enable/disable, run, and
            view logs.
          </p>
        )}
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : schedules.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {readOnly
              ? "No schedules. The bundle publisher hasn't shipped any schedules with this agent."
              : "No schedules yet. Create a Static Prompt or Script Trigger schedule to automate this agent."}
          </p>
        ) : (
          <div className="space-y-1.5">
            {schedules.map((schedule) => (
              <div
                key={schedule.id}
                className={`flex items-center justify-between px-3 py-2 border rounded-lg ${
                  !schedule.enabled ? "opacity-50 bg-muted" : ""
                }`}
              >
                {/* Left: name, description, next execution */}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium text-sm truncate">
                      {schedule.name}
                    </span>
                    {schedule.enabled ? (
                      <Badge className="text-xs shrink-0 bg-emerald-500 hover:bg-emerald-600">
                        Enabled
                      </Badge>
                    ) : (
                      <Badge variant="secondary" className="text-xs shrink-0">
                        Disabled
                      </Badge>
                    )}
                    {schedule.schedule_type === "script_trigger" && (
                      <Badge
                        variant="outline"
                        className="text-xs shrink-0 text-amber-700 border-amber-300 bg-amber-50"
                      >
                        <Terminal className="h-3 w-3 mr-1" />
                        Script trigger
                      </Badge>
                    )}
                    {schedule.schedule_type === "static_prompt" &&
                      schedule.prompt && (
                        <Badge variant="outline" className="text-xs shrink-0">
                          Custom prompt
                        </Badge>
                      )}
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {schedule.description}
                  </div>
                  {schedule.schedule_type === "script_trigger" &&
                    schedule.command && (
                      <div className="text-xs text-muted-foreground mt-0.5 font-mono truncate max-w-xs">
                        {schedule.command.length > 60
                          ? schedule.command.slice(0, 60) + "…"
                          : schedule.command}
                      </div>
                    )}
                  {schedule.enabled && schedule.next_execution && (
                    <div className="flex items-center gap-1 text-xs text-muted-foreground mt-0.5">
                      <Clock className="h-3 w-3" />
                      Next: {formatNextExecution(schedule.next_execution)}
                    </div>
                  )}
                </div>
                {/* Right: action buttons */}
                <div className="flex items-center gap-0.5 ml-2 shrink-0">
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={() => runNowMutation.mutate(schedule.id)}
                          disabled={runNowMutation.isPending}
                        >
                          <Play className="h-3.5 w-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Run now
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={() => handleShowLogs(schedule)}
                        >
                          <History className="h-3.5 w-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Execution logs
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  {!readOnly && (
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6"
                            onClick={() => handleEditOpen(schedule)}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="text-xs">
                          Edit schedule
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  )}
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={() =>
                            toggleMutation.mutate({
                              scheduleId: schedule.id,
                              enabled: !schedule.enabled,
                            })
                          }
                        >
                          {schedule.enabled ? (
                            <Power className="h-3.5 w-3.5 text-emerald-500" />
                          ) : (
                            <PowerOff className="h-3.5 w-3.5 text-muted-foreground" />
                          )}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        {schedule.enabled ? "Disable" : "Enable"}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  {!readOnly && (
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6 text-destructive hover:text-destructive"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>Delete Schedule</AlertDialogTitle>
                          <AlertDialogDescription>
                            Are you sure you want to delete &quot;{schedule.name}
                            &quot;? This action cannot be undone.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction
                            onClick={() => deleteMutation.mutate(schedule.id)}
                            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                          >
                            Delete
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Schedule</DialogTitle>
            <DialogDescription>
              Update the schedule name, timing, or{" "}
              {editingSchedule?.schedule_type === "script_trigger"
                ? "command"
                : "prompt"}
              .
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="edit-schedule-name">Name</Label>
              <Input
                id="edit-schedule-name"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Timing</Label>
              {/* Show current schedule info */}
              {editingSchedule && !editGenerated && (
                <div className="bg-secondary p-3 rounded-md space-y-1.5">
                  <div className="flex items-start gap-2">
                    <Check className="h-4 w-4 text-green-600 mt-0.5 shrink-0" />
                    <span className="font-medium text-sm">
                      {editingSchedule.description}
                    </span>
                  </div>
                  {editingSchedule.next_execution && (
                    <div className="flex items-start gap-2 text-sm text-muted-foreground">
                      <Clock className="h-4 w-4 mt-0.5 shrink-0" />
                      <span>
                        Next:{" "}
                        {formatNextExecution(editingSchedule.next_execution)}
                      </span>
                    </div>
                  )}
                </div>
              )}
              <div className="flex gap-2">
                <Input
                  value={editInput}
                  onChange={(e) => setEditInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleEditGenerate()}
                  placeholder={
                    editingSchedule
                      ? "Enter new timing to change schedule..."
                      : "e.g., every workday at 7am"
                  }
                  disabled={generateMutation.isPending}
                />
                <Button
                  onClick={handleEditGenerate}
                  disabled={!editInput.trim() || generateMutation.isPending}
                  variant="outline"
                >
                  {generateMutation.isPending ? (
                    "..."
                  ) : (
                    <>
                      <Sparkles className="h-4 w-4 mr-1" />
                      Generate
                    </>
                  )}
                </Button>
              </div>
              {editGenerated && (
                <div className="bg-secondary p-3 rounded-md space-y-1.5">
                  <div className="flex items-start gap-2">
                    <AlertCircle className="h-4 w-4 text-blue-500 mt-0.5 shrink-0" />
                    <span className="font-medium text-sm">
                      New: {editGenerated.description}
                    </span>
                  </div>
                  {editGenerated.next_execution && (
                    <div className="flex items-start gap-2 text-sm text-muted-foreground">
                      <Clock className="h-4 w-4 mt-0.5 shrink-0" />
                      <span>
                        Next: {formatNextExecution(editGenerated.next_execution)}
                      </span>
                    </div>
                  )}
                </div>
              )}
              {editError && (
                <div className="bg-destructive/10 border border-destructive/20 p-3 rounded-md">
                  <div className="flex items-start gap-2">
                    <AlertCircle className="h-4 w-4 text-destructive mt-0.5 shrink-0" />
                    <span className="text-sm text-destructive">{editError}</span>
                  </div>
                </div>
              )}
            </div>

            {editingSchedule?.schedule_type === "script_trigger" ? (
              <div className="space-y-2">
                <Label htmlFor="edit-schedule-command">Command</Label>
                <Input
                  id="edit-schedule-command"
                  value={editCommand}
                  onChange={(e) => setEditCommand(e.target.value)}
                  maxLength={2000}
                  className="font-mono text-sm"
                  placeholder="e.g., bash scripts/check_status.sh"
                />
                <p className="text-xs text-muted-foreground">
                  If output is &quot;OK&quot;, no session is started. Any other
                  output triggers a new agent session.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                <Label htmlFor="edit-schedule-prompt">Prompt (optional)</Label>
                <Textarea
                  id="edit-schedule-prompt"
                  placeholder="Leave empty to use agent's entrypoint prompt"
                  value={editPrompt}
                  onChange={(e) => setEditPrompt(e.target.value)}
                  rows={3}
                />
              </div>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEditDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button
              onClick={handleEditSave}
              disabled={!editName.trim() || updateMutation.isPending}
            >
              {updateMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Execution Logs Modal */}
      <Dialog open={logsModalOpen} onOpenChange={setLogsModalOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <History className="h-5 w-5" />
              Execution Logs
              {logsSchedule && (
                <span className="font-normal text-muted-foreground">
                  — &quot;{logsSchedule.name}&quot;
                </span>
              )}
            </DialogTitle>
            <DialogDescription>
              Last 50 execution records for this schedule.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[60vh] overflow-y-auto space-y-2 pr-1">
            {logsLoading ? (
              <p className="text-sm text-muted-foreground text-center py-8">
                Loading logs...
              </p>
            ) : logs.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-8">
                No execution logs yet. Logs appear after the first scheduled
                execution.
              </p>
            ) : (
              logs.map((log) => (
                <LogDetailRow
                  key={log.id as unknown as string}
                  log={log}
                />
              ))
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setLogsModalOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
