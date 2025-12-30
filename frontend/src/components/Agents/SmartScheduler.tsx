import { useState, useEffect } from "react"
import { useMutation } from "@tanstack/react-query"
import { Check, Clock, AlertCircle, Sparkles } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Alert, AlertDescription } from "@/components/ui/alert"
import useCustomToast from "@/hooks/useCustomToast"
import { AgentsService } from "@/client"
import type { AgentSchedulePublic } from "@/client"

interface SmartSchedulerProps {
  agentId: string
  currentSchedule?: AgentSchedulePublic
  onScheduleUpdate?: () => void
}

export function SmartScheduler({
  agentId,
  currentSchedule,
  onScheduleUpdate,
}: SmartSchedulerProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // State management
  const [enabled, setEnabled] = useState(currentSchedule?.enabled || false)
  const [input, setInput] = useState("")
  const [schedule, setSchedule] = useState<{
    success: boolean
    description?: string
    cron_string?: string
    next_execution?: string
    error?: string
  } | null>(null)
  const [hasChanges, setHasChanges] = useState(false)

  // Get user timezone
  const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone

  // Sync schedule state with currentSchedule prop
  useEffect(() => {
    if (currentSchedule && currentSchedule.enabled) {
      setEnabled(true)
      setSchedule({
        success: true,
        description: currentSchedule.description,
        cron_string: currentSchedule.cron_string,
        next_execution: currentSchedule.next_execution,
      })
    }
  }, [currentSchedule])

  // API calls
  const generateMutation = useMutation({
    mutationFn: (naturalLanguage: string) =>
      AgentsService.generateSchedule({
        id: agentId,
        requestBody: { natural_language: naturalLanguage, timezone: userTimezone },
      }),
    onSuccess: (data) => {
      setSchedule(data)
      if (data.success) {
        setHasChanges(true)
      }
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to generate schedule")
    },
  })

  const saveMutation = useMutation({
    mutationFn: (data: {
      cron_string: string
      description: string
      enabled: boolean
    }) =>
      AgentsService.saveSchedule({
        id: agentId,
        requestBody: {
          cron_string: data.cron_string,
          timezone: userTimezone,
          description: data.description,
          enabled: data.enabled,
        },
      }),
    onSuccess: () => {
      setHasChanges(false)
      showSuccessToast("Schedule saved successfully")
      onScheduleUpdate?.()
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to save schedule")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => AgentsService.deleteSchedule({ id: agentId }),
    onSuccess: () => {
      setSchedule(null)
      setEnabled(false)
      setInput("")
      setHasChanges(false)
      showSuccessToast("Schedule disabled")
      onScheduleUpdate?.()
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete schedule")
    },
  })

  const handleSchedule = () => {
    if (!input.trim()) return
    generateMutation.mutate(input)
  }

  const handleApply = () => {
    if (schedule?.success && schedule.cron_string && schedule.description) {
      saveMutation.mutate({
        cron_string: schedule.cron_string,
        description: schedule.description,
        enabled: true,
      })
    }
  }

  const handleToggle = (checked: boolean) => {
    setEnabled(checked)

    if (!checked && currentSchedule) {
      // Disable existing schedule
      deleteMutation.mutate()
    } else if (checked && currentSchedule) {
      // Re-enable with current schedule data
      setSchedule({
        success: true,
        description: currentSchedule.description,
        cron_string: currentSchedule.cron_string,
        next_execution: currentSchedule.next_execution,
      })
    }
  }

  const formatNextExecution = (isoDate?: string) => {
    if (!isoDate) return "Unknown"
    try {

      // Ensure the string is treated as UTC by appending 'Z' if not present
      let utcDateString = isoDate
      if (!isoDate.endsWith('Z') && !isoDate.includes('+') && !isoDate.includes('T')) {
        // Just a date without time, not our case
        utcDateString = isoDate
      } else if (!isoDate.endsWith('Z') && !isoDate.includes('+') && isoDate.includes('T')) {
        // Has time but no timezone indicator - assume UTC
        utcDateString = isoDate + 'Z'
      }

      const date = new Date(utcDateString)

      // Display in user's browser timezone
      const formatted = date.toLocaleString(undefined, {
        weekday: "long",
        year: "numeric",
        month: "long",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "short",
      })

      return formatted
    } catch (error) {
      return isoDate
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Label className="text-base font-semibold">Scheduler</Label>

        {/* Enable/Disable Toggle */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {enabled ? "Enabled" : "Disabled"}
          </span>
          <label className="flex cursor-pointer select-none items-center">
            <div className="relative">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => handleToggle(e.target.checked)}
                className="sr-only"
              />
              <div
                className={`block h-6 w-11 rounded-full transition-colors ${
                  enabled ? "bg-orange-400" : "bg-gray-300 dark:bg-gray-600"
                }`}
              ></div>
              <div
                className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                  enabled ? "translate-x-5" : ""
                }`}
              ></div>
            </div>
          </label>
        </div>
      </div>

      {/* Scheduler form - only visible when enabled */}
      {enabled && (
        <div className="space-y-4">
          <div className="flex gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSchedule()}
              placeholder="e.g., every workday in the morning at 7"
              disabled={generateMutation.isPending}
            />
            <Button
              onClick={handleSchedule}
              disabled={!input.trim() || generateMutation.isPending}
            >
              {generateMutation.isPending ? (
                "Processing..."
              ) : (
                <>
                  <Sparkles className="h-4 w-4 mr-2" />
                  Generate
                </>
              )}
            </Button>
          </div>

          {schedule?.success && schedule.description && (
            <div className="bg-secondary p-4 rounded-md space-y-2">
              <div className="flex items-start gap-2">
                <Check className="h-4 w-4 text-green-600 mt-0.5 flex-shrink-0" />
                <span className="font-medium text-sm">{schedule.description}</span>
              </div>
              {schedule.next_execution && (
                <div className="flex items-start gap-2 text-sm text-muted-foreground">
                  <Clock className="h-4 w-4 mt-0.5 flex-shrink-0" />
                  <span>Next run: {formatNextExecution(schedule.next_execution)}</span>
                </div>
              )}
            </div>
          )}

          {schedule?.error && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{schedule.error}</AlertDescription>
            </Alert>
          )}

          {hasChanges && schedule?.success && (
            <Button
              onClick={handleApply}
              className="w-full"
              disabled={saveMutation.isPending}
            >
              {saveMutation.isPending ? "Saving..." : "Apply Schedule"}
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
