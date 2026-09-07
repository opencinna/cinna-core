import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  AlertCircle,
  Check,
  Clock,
  FileText,
  Sparkles,
  Terminal,
} from "lucide-react"

import type { CreateScheduleRequest } from "@/client"
import { AgentsService } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { formatNextExecution } from "./scheduleFormatting"

type ScheduleType = "static_prompt" | "script_trigger"
type CreateStep = "type" | "details"

interface GeneratedTiming {
  description: string
  cron_string: string
  next_execution: string
}

interface CreateScheduleDialogProps {
  agentId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Set up a timed run fast" (§1 Create).
 *
 * The form's shape depends on the type — a prompt versus a command — so
 * choosing the type **is** step 1 (P4), and the type-specific form is step 2
 * of a two-step wizard (P6). Never a `<Select type>` at the top of one long
 * form.
 *
 * Nothing is created until Create is pressed: the type is carried in state
 * rather than materialised as a draft entity, because a schedule with no
 * timing has no meaningful existence.
 *
 * Not rendered at all under `readOnly` — a bundle consumer cannot create
 * schedules, so the trigger and this dialog are both absent.
 */
export function CreateScheduleDialog({
  agentId,
  open,
  onOpenChange,
}: CreateScheduleDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone

  const [step, setStep] = useState<CreateStep>("type")
  const [type, setType] = useState<ScheduleType>("static_prompt")
  const [name, setName] = useState("")
  const [timingInput, setTimingInput] = useState("")
  const [prompt, setPrompt] = useState("")
  const [command, setCommand] = useState("")
  const [generated, setGenerated] = useState<GeneratedTiming | null>(null)
  const [generateError, setGenerateError] = useState<string | null>(null)

  const resetForm = () => {
    setStep("type")
    setType("static_prompt")
    setName("")
    setTimingInput("")
    setPrompt("")
    setCommand("")
    setGenerated(null)
    setGenerateError(null)
  }

  const generateMutation = useMutation({
    mutationFn: (naturalLanguage: string) =>
      AgentsService.generateSchedule({
        id: agentId,
        requestBody: {
          natural_language: naturalLanguage,
          timezone: userTimezone,
          schedule_type: type,
        },
      }),
  })

  const createMutation = useMutation({
    mutationFn: (body: CreateScheduleRequest) =>
      AgentsService.createSchedule({ id: agentId, requestBody: body }),
    onSuccess: () => {
      showSuccessToast("Schedule created")
      queryClient.invalidateQueries({ queryKey: ["agent-schedules", agentId] })
      resetForm()
      onOpenChange(false)
    },
    onError: (err: unknown) =>
      showErrorToast(getErrorMessage(err, "Failed to create schedule")),
  })

  const close = () => {
    resetForm()
    onOpenChange(false)
  }

  const handleTypeSelect = (chosen: ScheduleType) => {
    // A generated cadence was validated against the *previous* type's floor —
    // script triggers have none, static prompts have a ten-minute minimum — so
    // it cannot carry across a type change. Leaving it would show a confirmed
    // timing that the create call then rejects, with nothing on screen saying
    // why. The same goes for an error raised against the old type, and for a
    // generate still in flight when the type changed.
    if (chosen !== type) {
      setGenerated(null)
      setGenerateError(null)
      generateMutation.reset()
    }
    setType(chosen)
    setStep("details")
  }

  const handleGenerate = async () => {
    if (!timingInput.trim()) return
    setGenerateError(null)
    setGenerated(null)
    try {
      const result = await generateMutation.mutateAsync(timingInput)
      if (result.success && result.cron_string && result.description) {
        setGenerated({
          description: result.description,
          cron_string: result.cron_string,
          next_execution: result.next_execution ?? "",
        })
      } else {
        setGenerateError(result.error || "Failed to generate schedule")
      }
    } catch (err: unknown) {
      setGenerateError(getErrorMessage(err, "Failed to generate schedule"))
    }
  }

  const isScript = type === "script_trigger"

  const handleCreate = () => {
    if (!name.trim() || !generated) return
    createMutation.mutate({
      name: name.trim(),
      cron_string: generated.cron_string,
      timezone: userTimezone,
      description: generated.description,
      prompt: isScript ? null : prompt.trim() || null,
      enabled: true,
      schedule_type: type,
      command: isScript ? command.trim() : null,
    })
  }

  const isCreateDisabled =
    !name.trim() ||
    !generated ||
    generateMutation.isPending ||
    (isScript && !command.trim())

  return (
    <Dialog
      open={open}
      // Escape and outside clicks must not close the dialog mid-request.
      onOpenChange={(next) => {
        if (next) onOpenChange(true)
        else if (!createMutation.isPending) close()
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          {/* Two hidden panes give no sense that there is a second step. */}
          <p className="text-xs text-muted-foreground">
            <span
              className={
                step === "type" ? "font-medium text-foreground" : undefined
              }
            >
              1 Type
            </span>
            {" · "}
            <span
              className={
                step === "details" ? "font-medium text-foreground" : undefined
              }
            >
              2 Details
            </span>
          </p>
          <DialogTitle>
            {step === "type"
              ? "New schedule"
              : isScript
                ? "Create script trigger schedule"
                : "Create static prompt schedule"}
          </DialogTitle>
          <DialogDescription>
            {step === "type"
              ? "Choose the type of schedule to create."
              : "Name it, say when it should run, and give it something to do."}
          </DialogDescription>
        </DialogHeader>

        {step === "type" ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 py-2">
            <button
              type="button"
              onClick={() => handleTypeSelect("static_prompt")}
              className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
            >
              <div className="flex items-center gap-2">
                <FileText className="h-5 w-5 text-primary" />
                <span className="font-medium text-sm">Static prompt</span>
              </div>
              <p className="text-xs text-muted-foreground leading-relaxed">
                Best for agentic workflows that always need a conversational
                starting point. A session is created every run, so each
                execution consumes tokens.
              </p>
            </button>

            <button
              type="button"
              onClick={() => handleTypeSelect("script_trigger")}
              className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
            >
              <div className="flex items-center gap-2">
                <Terminal className="h-5 w-5 text-amber-600" />
                <span className="font-medium text-sm">Script trigger</span>
              </div>
              <p className="text-xs text-muted-foreground leading-relaxed">
                Best for direct scenarios covered by a predefined script. The
                agent is only involved when something needs attention, so if
                everything runs smoothly it may not consume any tokens at all.
              </p>
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="schedule-name">Name</Label>
              <Input
                id="schedule-name"
                placeholder="e.g., Daily health check"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="schedule-timing">Timing</Label>
              <div className="flex gap-2">
                <Input
                  id="schedule-timing"
                  value={timingInput}
                  onChange={(e) => setTimingInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleGenerate()}
                  placeholder="e.g., every workday at 7am"
                  disabled={generateMutation.isPending}
                />
                <Button
                  onClick={handleGenerate}
                  disabled={!timingInput.trim() || generateMutation.isPending}
                  variant="outline"
                >
                  {generateMutation.isPending ? (
                    "Generating…"
                  ) : (
                    <>
                      <Sparkles className="h-4 w-4 mr-1" />
                      Generate
                    </>
                  )}
                </Button>
              </div>
              {generated && (
                <div className="bg-secondary p-3 rounded-md space-y-1.5">
                  <div className="flex items-start gap-2">
                    <Check className="h-4 w-4 text-green-600 mt-0.5 shrink-0" />
                    <span className="font-medium text-sm">
                      {generated.description}
                    </span>
                  </div>
                  {generated.next_execution && (
                    <div className="flex items-start gap-2 text-sm text-muted-foreground">
                      <Clock className="h-4 w-4 mt-0.5 shrink-0" />
                      <span>
                        Next: {formatNextExecution(generated.next_execution)}
                      </span>
                    </div>
                  )}
                </div>
              )}
              {generateError && (
                <Alert variant="destructive">
                  <AlertCircle />
                  <AlertTitle>Couldn&apos;t work out that timing</AlertTitle>
                  <AlertDescription>{generateError}</AlertDescription>
                </Alert>
              )}
            </div>

            {isScript ? (
              <div className="space-y-2">
                <Label htmlFor="schedule-command">Command</Label>
                <Input
                  id="schedule-command"
                  placeholder="e.g., bash scripts/check_status.sh"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  maxLength={2000}
                  className="font-mono text-sm"
                />
                <p className="text-xs text-muted-foreground">
                  Command to execute inside the agent environment. If it returns
                  &quot;OK&quot;, no session is started. Any other output
                  triggers a new agent session with the execution context.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                <Label htmlFor="schedule-prompt">Prompt (optional)</Label>
                <Textarea
                  id="schedule-prompt"
                  placeholder="Leave empty to use the agent's entrypoint prompt"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  rows={3}
                />
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          {step === "type" ? (
            <Button type="button" variant="outline" onClick={close}>
              Cancel
            </Button>
          ) : (
            <>
              {/* Back belongs in the footer with the other controls, not
                  inside the dialog's description. */}
              <Button
                type="button"
                variant="outline"
                disabled={createMutation.isPending}
                onClick={() => setStep("type")}
              >
                Back
              </Button>
              <LoadingButton
                loading={createMutation.isPending}
                disabled={isCreateDisabled}
                onClick={handleCreate}
              >
                {createMutation.isPending ? "Creating…" : "Create"}
              </LoadingButton>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
