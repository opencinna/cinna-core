import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useMemo, useRef, useCallback, forwardRef } from "react"

import { ActivitiesService, SessionsService, TasksService } from "@/client"
import type { ActivityPublicExtended, SessionPublicExtended, InputTaskPublicExtended } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import { getColorPreset } from "@/utils/colorPresets"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { AnimatedPlaceholder } from "@/components/Common/AnimatedPlaceholder"
import {
  Bell,
  CheckCircle2,
  AlertCircle,
  ClipboardList,
  FileText,
  MessageCircle,
  AlertOctagon,
  Loader2,
  EllipsisVertical,
  Archive,
  List,
  HelpCircle,
  Mail,
  Wrench,
  XCircle,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { useMultiEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"

export const Route = createFileRoute("/_layout/activities")({
  component: ActivitiesList,
})

// ─── Types ────────────────────────────────────────────────────────────────────

type HappeningItem =
  | { kind: "task_with_session"; task: InputTaskPublicExtended; session: SessionPublicExtended }
  | { kind: "task_only"; task: InputTaskPublicExtended }
  | { kind: "session_only"; session: SessionPublicExtended }

// ─── Constants ────────────────────────────────────────────────────────────────

const STATUS_DOT_COLORS: Record<string, string> = {
  new: "bg-gray-400",
  refining: "bg-purple-500",
  open: "bg-gray-400",
  in_progress: "bg-blue-500",
  blocked: "bg-amber-500",
  completed: "bg-green-500",
  error: "bg-red-500",
  cancelled: "bg-red-400",
  archived: "bg-gray-400",
  running: "bg-blue-500",
  pending_input: "bg-amber-500",
}

const DATE_GROUP_ORDER = ["Today", "Yesterday", "Last 7 days", "Older"]

// ─── Helpers ──────────────────────────────────────────────────────────────────

function getDateGroup(dateStr: string): string {
  const date = new Date(dateStr.endsWith("Z") ? dateStr : dateStr + "Z")
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)
  const lastWeek = new Date(today)
  lastWeek.setDate(lastWeek.getDate() - 7)

  if (date >= today) return "Today"
  if (date >= yesterday) return "Yesterday"
  if (date >= lastWeek) return "Last 7 days"
  return "Older"
}

function groupActivitiesByDate(
  activities: ActivityPublicExtended[],
): { label: string; activities: ActivityPublicExtended[] }[] {
  const groups: Record<string, ActivityPublicExtended[]> = {}
  for (const activity of activities) {
    const group = getDateGroup(activity.created_at)
    ;(groups[group] ??= []).push(activity)
  }
  return DATE_GROUP_ORDER
    .filter((label) => groups[label]?.length)
    .map((label) => ({ label, activities: groups[label] }))
}

function getActivityIcon(activityType: string): { icon: React.ReactNode; colorClass: string } {
  switch (activityType) {
    case "session_running":
      return { icon: <Loader2 className="h-4 w-4 animate-spin" />, colorClass: "text-emerald-500" }
    case "session_completed":
      return { icon: <CheckCircle2 className="h-4 w-4" />, colorClass: "text-green-500" }
    case "task_completed":
      return { icon: <CheckCircle2 className="h-4 w-4" />, colorClass: "text-green-500" }
    case "questions_asked":
      return { icon: <MessageCircle className="h-4 w-4" />, colorClass: "text-amber-500" }
    case "session_feedback_required":
      return { icon: <HelpCircle className="h-4 w-4" />, colorClass: "text-amber-500" }
    case "error_occurred":
    case "task_failed":
      return { icon: <AlertOctagon className="h-4 w-4" />, colorClass: "text-red-500" }
    case "file_created":
      return { icon: <FileText className="h-4 w-4" />, colorClass: "text-blue-500" }
    case "agent_notification":
      return { icon: <Bell className="h-4 w-4" />, colorClass: "text-blue-500" }
    case "email_task_incoming":
    case "email_task_reply_pending":
      return { icon: <Mail className="h-4 w-4" />, colorClass: "text-purple-500" }
    case "task_blocked":
      return { icon: <AlertCircle className="h-4 w-4" />, colorClass: "text-amber-500" }
    case "task_cancelled":
      return { icon: <XCircle className="h-4 w-4" />, colorClass: "text-red-400" }
    default:
      return { icon: <Bell className="h-4 w-4" />, colorClass: "text-muted-foreground" }
  }
}

function getActionRequiredLabel(actionRequired: string): string {
  switch (actionRequired) {
    case "answers_required":
      return "Answers required"
    case "task_review_required":
      return "Review required"
    case "reply_pending":
      return "Reply pending"
    case "task_action_required":
      return "Task blocked"
    default:
      return actionRequired.replace(/_/g, " ")
  }
}

/** Entity-type icon shown inline before the activity text. */
function getEntityIcon(activityType: string): React.ReactNode {
  if (activityType.startsWith("task_") || activityType.startsWith("email_task_")) {
    return <ClipboardList className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
  }
  // Session-related activities — default to conversation icon
  return <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function SectionHeader({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <h2 className="text-sm font-semibold text-foreground">{label}</h2>
      {count > 0 && (
        <span className="inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full text-xs font-medium bg-primary/10 text-primary">
          {count}
        </span>
      )}
    </div>
  )
}

function ActionRequiredRow({
  activity,
  onClick,
}: {
  activity: ActivityPublicExtended
  onClick: () => void
}) {
  const colorPreset = getColorPreset(activity.agent_ui_color_preset)
  const title = activity.session_title || null

  return (
    <div
      onClick={onClick}
      className="flex items-start gap-3 px-3 py-2.5 cursor-pointer transition-colors hover:bg-muted/50 bg-destructive/5"
    >
      <div className="mt-0.5 shrink-0 text-destructive">
        <AlertCircle className="h-4 w-4" />
      </div>
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-center gap-2 flex-wrap">
          {activity.agent_name && (
            <span
              className={cn(
                "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium shrink-0",
                colorPreset.badgeBg,
                colorPreset.badgeText,
              )}
            >
              {activity.agent_name}
            </span>
          )}
          {title && (
            <span className="text-xs text-muted-foreground truncate">
              {title}
            </span>
          )}
          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-destructive/10 text-destructive shrink-0">
            {getActionRequiredLabel(activity.action_required)}
          </span>
        </div>
        <p className="text-sm text-foreground flex items-center gap-1.5">
          {getEntityIcon(activity.activity_type)}
          {activity.text}
        </p>
      </div>
      <RelativeTime timestamp={activity.created_at} className="text-xs text-muted-foreground shrink-0 mt-0.5" />
    </div>
  )
}

function TaskWithSessionRow({
  item,
  onClick,
}: {
  item: { kind: "task_with_session"; task: InputTaskPublicExtended; session: SessionPublicExtended }
  onClick: () => void
}) {
  const { task, session } = item
  const colorPreset = getColorPreset(session.agent_ui_color_preset ?? null)

  return (
    <div
      onClick={onClick}
      className="flex items-center gap-3 px-3 py-2.5 cursor-pointer transition-colors hover:bg-muted/50"
    >
      <Loader2 className="h-4 w-4 animate-spin text-emerald-500 shrink-0" />
      <span className="text-xs font-mono text-muted-foreground shrink-0 w-16 truncate">
        {task.short_code ?? "—"}
      </span>
      <span className="text-sm flex-1 min-w-0 truncate">
        {task.title || task.current_description}
      </span>
      {session.agent_name && (
        <span
          className={cn(
            "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium shrink-0",
            colorPreset.badgeBg,
            colorPreset.badgeText,
          )}
        >
          {session.agent_name}
        </span>
      )}
      <span className="text-xs text-muted-foreground truncate shrink-0 max-w-[160px] hidden sm:block">
        {session.title ? session.title : <AnimatedPlaceholder className="text-xs" />}
      </span>
      <RelativeTime
        timestamp={session.last_message_at || session.updated_at}
        className="text-xs text-muted-foreground shrink-0"
      />
    </div>
  )
}

function TaskOnlyRow({
  item,
  onClick,
}: {
  item: { kind: "task_only"; task: InputTaskPublicExtended }
  onClick: () => void
}) {
  const { task } = item

  return (
    <div
      onClick={onClick}
      className="flex items-center gap-3 px-3 py-2.5 cursor-pointer transition-colors hover:bg-muted/50"
    >
      <span
        className={cn(
          "h-2 w-2 rounded-full shrink-0",
          STATUS_DOT_COLORS[task.status] ?? "bg-gray-400",
        )}
      />
      <span className="text-xs font-mono text-muted-foreground shrink-0 w-16 truncate">
        {task.short_code ?? "—"}
      </span>
      <span className="text-sm flex-1 min-w-0 truncate">
        {task.title || task.current_description}
      </span>
      {task.agent_name && (
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium shrink-0 bg-muted text-muted-foreground">
          {task.agent_name}
        </span>
      )}
      <RelativeTime
        timestamp={task.updated_at}
        className="text-xs text-muted-foreground shrink-0"
      />
    </div>
  )
}

function SessionOnlyRow({
  item,
  onClick,
}: {
  item: { kind: "session_only"; session: SessionPublicExtended }
  onClick: () => void
}) {
  const { session } = item
  const colorPreset = getColorPreset(session.agent_ui_color_preset ?? null)
  const isPending = session.interaction_status === "pending_stream"

  return (
    <div
      onClick={onClick}
      className="flex items-center gap-3 px-3 py-2.5 cursor-pointer transition-colors hover:bg-muted/50"
    >
      {session.mode === "building" ? (
        <Wrench className="h-4 w-4 text-orange-500 shrink-0" />
      ) : (
        <MessageCircle className="h-4 w-4 text-blue-500 shrink-0" />
      )}
      <span className="text-sm flex-1 min-w-0 truncate">
        {session.title ? session.title : <AnimatedPlaceholder className="text-xs" />}
      </span>
      {session.agent_name && (
        <span
          className={cn(
            "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium shrink-0",
            colorPreset.badgeBg,
            colorPreset.badgeText,
          )}
        >
          {session.agent_name}
        </span>
      )}
      {isPending && (
        <span className="text-[10px] text-amber-500 font-medium shrink-0">Starting...</span>
      )}
      <RelativeTime
        timestamp={session.last_message_at || session.updated_at}
        className="text-xs text-muted-foreground shrink-0"
      />
    </div>
  )
}

const LogEntryRow = forwardRef<
  HTMLDivElement,
  {
    activity: ActivityPublicExtended
    onClick: () => void
    "data-activity-id": string
  }
>(function LogEntryRow({ activity, onClick, ...rest }, ref) {
  const colorPreset = getColorPreset(activity.agent_ui_color_preset)
  const hasActionRequired = activity.action_required !== ""
  const isUnread = !activity.is_read
  const isRunning = activity.activity_type === "session_running"
  const { icon, colorClass } = getActivityIcon(activity.activity_type)

  // Build context line: task short code + title, or session title
  const contextParts: string[] = []
  if (activity.task_short_code) contextParts.push(activity.task_short_code)
  if (activity.task_title) contextParts.push(activity.task_title)
  else if (activity.session_title) contextParts.push(activity.session_title)
  const contextText = contextParts.length > 0 ? contextParts.join(" · ") : null

  return (
    <div
      ref={ref}
      {...rest}
      onClick={onClick}
      className={cn(
        "relative flex items-start gap-3 px-3 py-2.5 transition-colors",
        (activity.session_id || activity.input_task_id)
          ? "cursor-pointer hover:bg-muted/50"
          : "",
        isRunning
          ? "bg-emerald-500/10"
          : isUnread
            ? "bg-primary/8 border-l-2 border-l-primary"
            : "",
      )}
    >
      <div className={cn("mt-0.5 shrink-0", colorClass)}>
        {icon}
      </div>
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm text-foreground flex items-center gap-1.5">
            {getEntityIcon(activity.activity_type)}
            {activity.text}
            {isUnread && (
              <span
                className={cn(
                  "inline-block w-1.5 h-1.5 rounded-full shrink-0",
                  hasActionRequired ? "bg-destructive" : "bg-primary",
                )}
              />
            )}
          </p>
          <RelativeTime timestamp={activity.created_at} className="text-xs text-muted-foreground shrink-0" />
        </div>
        <div className="flex items-center justify-between gap-2">
          {contextText ? (
            <span className="text-xs text-muted-foreground truncate">
              {contextText}
            </span>
          ) : <span />}
          {activity.agent_name && (
            <span
              className={cn(
                "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium shrink-0",
                colorPreset.badgeBg,
                colorPreset.badgeText,
              )}
            >
              {activity.agent_name}
            </span>
          )}
        </div>
      </div>
    </div>
  )
})

// ─── Main Component ───────────────────────────────────────────────────────────

function ActivitiesList() {
  const { setHeaderContent } = usePageHeader()
  const [menuOpen, setMenuOpen] = useState(false)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Intersection observer for auto-mark-as-read (log entries only)
  const [visibleActivities, setVisibleActivities] = useState<Set<string>>(new Set())
  const observerRef = useRef<IntersectionObserver | null>(null)
  const activityRefs = useRef<Map<string, HTMLDivElement>>(new Map())

  // ── Queries ──────────────────────────────────────────────────────────────────

  // All activities — cross-workspace (no userWorkspaceId param)
  const {
    data: activitiesData,
    isLoading: activitiesLoading,
    error: activitiesError,
  } = useQuery({
    queryKey: ["activities-all"],
    queryFn: () =>
      ActivitiesService.listActivities({
        skip: 0,
        limit: 200,
        orderDesc: true,
      }),
    placeholderData: (previousData) => previousData,
  })

  // Sessions — cross-workspace, poll every 15s
  const { data: sessionsData } = useQuery({
    queryKey: ["sessions-active"],
    queryFn: () =>
      SessionsService.listSessions({
        limit: 50,
        orderBy: "last_message_at",
        orderDesc: true,
      }),
    refetchInterval: 15000,
  })

  // In-progress tasks — cross-workspace, poll every 15s
  const { data: tasksData } = useQuery({
    queryKey: ["tasks-in-progress"],
    queryFn: () =>
      TasksService.listTasks({
        status: "in_progress",
      }),
    refetchInterval: 15000,
  })

  // ── Mutations ────────────────────────────────────────────────────────────────

  const markAsReadMutation = useMutation({
    mutationFn: (activityIds: string[]) =>
      ActivitiesService.markActivitiesAsRead({ requestBody: activityIds }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["activities-all"] })
      queryClient.invalidateQueries({ queryKey: ["activity-stats"] })
    },
  })

  const archiveLogsMutation = useMutation({
    mutationFn: () => ActivitiesService.archiveLogs(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["activities-all"] })
      queryClient.invalidateQueries({ queryKey: ["activity-stats"] })
      showSuccessToast(`Archived ${(data as any).archived_count} logs`)
    },
    onError: () => {
      showErrorToast("Failed to archive logs")
    },
  })

  // ── WebSocket subscriptions ──────────────────────────────────────────────────

  useMultiEventSubscription(
    [
      EventTypes.ACTIVITY_CREATED,
      EventTypes.ACTIVITY_UPDATED,
      EventTypes.ACTIVITY_DELETED,
      EventTypes.SESSION_INTERACTION_STATUS_CHANGED,
      EventTypes.SESSION_STATE_UPDATED,
      EventTypes.TASK_STATUS_CHANGED,
    ],
    () => {
      queryClient.invalidateQueries({ queryKey: ["activities-all"] })
      queryClient.invalidateQueries({ queryKey: ["sessions-active"] })
      queryClient.invalidateQueries({ queryKey: ["tasks-in-progress"] })
      queryClient.invalidateQueries({ queryKey: ["activity-stats"] })
    },
  )

  // ── Header ───────────────────────────────────────────────────────────────────

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center justify-between w-full gap-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Activities</h1>
          <p className="text-xs text-muted-foreground">System heartbeat — all workspaces</p>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => archiveLogsMutation.mutate()}
            disabled={archiveLogsMutation.isPending}
            title="Archive Logs"
          >
            <Archive className="h-4 w-4 mr-1.5" />
            Archive Logs
          </Button>
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm">
                <EllipsisVertical className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() => {
                  setMenuOpen(false)
                  navigate({ to: "/activities-all" })
                }}
              >
                <List className="h-4 w-4 mr-2" />
                Show All Logs
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, menuOpen, archiveLogsMutation.isPending])

  // ── Intersection observer ────────────────────────────────────────────────────

  useEffect(() => {
    observerRef.current = new IntersectionObserver(
      (entries) => {
        // Use functional updater to avoid stale closure over visibleActivities
        setVisibleActivities((prev) => {
          const next = new Set(prev)
          entries.forEach((entry) => {
            const id = entry.target.getAttribute("data-activity-id")
            if (!id) return
            if (entry.isIntersecting) {
              next.add(id)
            } else {
              next.delete(id)
            }
          })
          return next
        })
      },
      { threshold: 1.0 },
    )
    return () => observerRef.current?.disconnect()
  }, [])

  // Auto-mark visible unread log entries as read after 2 seconds
  useEffect(() => {
    if (visibleActivities.size === 0) return
    const unread = Array.from(visibleActivities).filter((id) => {
      const activity = activitiesData?.data.find((a) => a.id === id)
      return activity && !activity.is_read
    })
    if (unread.length === 0) return
    const timer = setTimeout(() => {
      markAsReadMutation.mutate(unread)
    }, 4000)
    return () => clearTimeout(timer)
  }, [visibleActivities, activitiesData?.data])

  // Re-observe log entries when data changes
  useEffect(() => {
    if (!observerRef.current) return
    observerRef.current.disconnect()
    activityRefs.current.forEach((el) => {
      if (el) observerRef.current?.observe(el)
    })
  }, [activitiesData?.data])

  // ── Derived data ─────────────────────────────────────────────────────────────

  const actionRequiredItems = useMemo(
    () => (activitiesData?.data ?? []).filter((a) => a.action_required !== ""),
    [activitiesData?.data],
  )

  const happeningItems = useMemo((): HappeningItem[] => {
    const allSessions = sessionsData?.data ?? []
    const allTasks = tasksData?.data ?? []

    const activeSessions = allSessions.filter(
      (s) => s.interaction_status === "running" || s.interaction_status === "pending_stream",
    )

    const sessionById = new Map(activeSessions.map((s) => [s.id, s]))
    const claimedSessionIds = new Set<string>()
    const items: HappeningItem[] = []

    for (const task of allTasks) {
      const latestId = task.latest_session_id
      if (latestId && sessionById.has(latestId)) {
        items.push({ kind: "task_with_session", task, session: sessionById.get(latestId)! })
        claimedSessionIds.add(latestId)
      } else {
        items.push({ kind: "task_only", task })
      }
    }

    for (const session of activeSessions) {
      if (!claimedSessionIds.has(session.id) && !session.source_task_id) {
        items.push({ kind: "session_only", session })
      }
    }

    return items
  }, [sessionsData?.data, tasksData?.data])

  const dateGroups = useMemo(
    () => groupActivitiesByDate(activitiesData?.data ?? []),
    [activitiesData?.data],
  )

  // ── Navigation ───────────────────────────────────────────────────────────────

  const navigateToActivity = useCallback((activity: ActivityPublicExtended) => {
    if (activity.input_task_id) {
      navigate({ to: "/task/$taskId", params: { taskId: activity.input_task_id } })
    } else if (activity.session_id) {
      navigate({
        to: "/session/$sessionId",
        params: { sessionId: activity.session_id },
        search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined, pageContext: undefined },
      })
    }
  }, [navigate])

  const navigateToTask = useCallback((task: InputTaskPublicExtended) => {
    navigate({ to: "/task/$taskId", params: { taskId: task.short_code || task.id } })
  }, [navigate])

  const navigateToSession = useCallback((session: SessionPublicExtended) => {
    navigate({
      to: "/session/$sessionId",
      params: { sessionId: session.id },
      search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined, pageContext: undefined },
    })
  }, [navigate])

  // ── Loading / error ──────────────────────────────────────────────────────────

  if (activitiesLoading && !activitiesData) {
    return <PendingItems />
  }

  if (activitiesError) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading activities</p>
      </div>
    )
  }

  const logItems = activitiesData?.data ?? []

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div className="p-6 md:p-8 overflow-y-auto h-full">
      <div className="mx-auto max-w-4xl space-y-8">

        {/* Section 1: Requires Action — hidden when empty */}
        {actionRequiredItems.length > 0 && (
          <div>
            <SectionHeader label="Requires Action" count={actionRequiredItems.length} />
            <div className="border rounded-lg divide-y overflow-hidden">
              {actionRequiredItems.map((activity) => (
                <ActionRequiredRow
                  key={activity.id}
                  activity={activity}
                  onClick={() => navigateToActivity(activity)}
                />
              ))}
            </div>
          </div>
        )}

        {/* Section 2: Happening Now */}
        <div>
          <SectionHeader label="Happening Now" count={happeningItems.length} />
          {happeningItems.length === 0 ? (
            <div className="text-center py-8 border-2 border-dashed rounded-lg">
              <p className="text-sm text-muted-foreground">Nothing is running right now</p>
            </div>
          ) : (
            <div className="border rounded-lg divide-y overflow-hidden">
              {happeningItems.map((item) => {
                if (item.kind === "task_with_session") {
                  return (
                    <TaskWithSessionRow
                      key={item.task.id}
                      item={item}
                      onClick={() => navigateToTask(item.task)}
                    />
                  )
                }
                if (item.kind === "task_only") {
                  return (
                    <TaskOnlyRow
                      key={item.task.id}
                      item={item}
                      onClick={() => navigateToTask(item.task)}
                    />
                  )
                }
                return (
                  <SessionOnlyRow
                    key={item.session.id}
                    item={item}
                    onClick={() => navigateToSession(item.session)}
                  />
                )
              })}
            </div>
          )}
        </div>

        {/* Section 3: Logs — time-grouped */}
        <div>
          <SectionHeader label="Logs" count={logItems.length} />
          {logItems.length === 0 ? (
            <div className="text-center py-8 border-2 border-dashed rounded-lg">
              <p className="text-sm text-muted-foreground">No activity logs yet</p>
            </div>
          ) : (
            <div className="space-y-6">
              {dateGroups.map((group) => (
                <div key={group.label}>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2 px-1">
                    {group.label}
                  </h3>
                  <div className="border rounded-lg divide-y overflow-hidden">
                    {group.activities.map((activity) => (
                      <LogEntryRow
                        key={activity.id}
                        ref={(el) => {
                          if (el) {
                            activityRefs.current.set(activity.id, el)
                          } else {
                            activityRefs.current.delete(activity.id)
                          }
                        }}
                        data-activity-id={activity.id}
                        activity={activity}
                        onClick={() => navigateToActivity(activity)}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

      </div>
    </div>
  )
}
