import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, KeyboardEvent, useRef, useCallback, DragEvent } from "react"
import {
  Play,
  ArrowLeft,
  Edit,
  Loader2,
  Send,
  Check,
  EllipsisVertical,
  Trash2,
  Archive,
  Bot,
  X,
  TextSelect,
  Sparkles,
  Zap,
  Mail,
  User,
  Paperclip,
  ChevronDown,
  Download,
  FileText,
  MessageSquare,
  ListTree,
  Activity,
  GitBranchPlus,
  CheckCircle2,
  Circle,
  Clock,
  AlertCircle,
  Ban,
} from "lucide-react"

import { TasksService, AgentsService, AgenticTeamsService } from "@/client"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import type { InputTaskDetailPublic, TaskCommentPublic, TaskAttachmentPublic, SessionPublic, AgenticTeamNodePublic } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
import NotFound from "@/components/Common/NotFound"
import { usePageHeader } from "@/routes/_layout"
import { TaskSessionsModal } from "@/components/Tasks/TaskSessionsModal"
import { TriggerManagementModal } from "@/components/Tasks/Triggers/TriggerManagementModal"
import { TaskTriggersApi } from "@/components/Tasks/Triggers/triggerApi"
import { TaskStatusPill } from "@/components/Tasks/TaskStatusPill"
import { TaskShortCodeBadge } from "@/components/Tasks/TaskShortCodeBadge"
import { SubtaskProgressChip } from "@/components/Tasks/SubtaskProgressChip"
import { AgentSelectorDialog } from "@/components/Common/AgentSelectorDialog"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useWorkspace from "@/hooks/useWorkspace"
import useCustomToast from "@/hooks/useCustomToast"
import { useMultiEventSubscription, EventTypes } from "@/hooks/useEventBus"
import { downloadAuthenticatedFile } from "@/utils"
import { getColorPreset } from "@/utils/colorPresets"
import { getWorkspaceIcon } from "@/config/workspaceIcons"
import {
  Popover,
  PopoverTrigger,
  PopoverContent,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"

export const Route = createFileRoute("/_layout/task/$taskId")({
  component: TaskDetailPage,
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isUUID(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
}

function formatFileSize(bytes: number | null | undefined): string {
  if (!bytes) return ""
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

type BodyTab = "comments" | "sessions" | "subtasks" | "activity"

// Status icons for tree nodes
const treeStatusIcons: Record<string, React.ReactNode> = {
  new: <Sparkles className="h-3 w-3 text-gray-500" />,
  refining: <Edit className="h-3 w-3 text-purple-500" />,
  open: <Circle className="h-3 w-3 text-gray-500" />,
  in_progress: <Play className="h-3 w-3 text-blue-500" />,
  running: <Play className="h-3 w-3 text-blue-500" />,
  blocked: <Clock className="h-3 w-3 text-amber-500" />,
  pending_input: <Clock className="h-3 w-3 text-amber-500" />,
  completed: <CheckCircle2 className="h-3 w-3 text-green-500" />,
  error: <AlertCircle className="h-3 w-3 text-red-500" />,
  cancelled: <Ban className="h-3 w-3 text-red-500" />,
  archived: <Archive className="h-3 w-3 text-gray-400" />,
}

interface TaskTreeNode {
  id: string
  short_code: string | null
  title: string
  status: string
  subtasks: TaskTreeNode[]
}

function TaskTreePopover({ rootShortCode, currentTaskId }: { rootShortCode: string; currentTaskId: string }) {
  const navigate = useNavigate()
  const { data, isLoading } = useQuery({
    queryKey: ["task-tree", rootShortCode],
    queryFn: () => TasksService.getTaskTreeByCode({ shortCode: rootShortCode }),
  })

  const renderNode = (node: TaskTreeNode, depth: number) => {
    const isCurrent = node.id === currentTaskId
    return (
      <div key={node.id}>
        <button
          type="button"
          onClick={() => navigate({ to: "/task/$taskId", params: { taskId: node.short_code || node.id } })}
          className={cn(
            "w-full flex items-center gap-1.5 py-1 px-2 rounded text-left hover:bg-muted/50 transition-colors text-xs",
            isCurrent && "bg-primary/10 font-medium"
          )}
          style={{ paddingLeft: `${depth * 16 + 8}px` }}
        >
          {treeStatusIcons[node.status] ?? <Circle className="h-3 w-3 text-gray-500" />}
          {node.short_code && (
            <span className="font-mono text-muted-foreground shrink-0">{node.short_code}</span>
          )}
          <span className="truncate">{node.title}</span>
        </button>
        {node.subtasks?.map((child) => renderNode(child, depth + 1))}
      </div>
    )
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
          title="View task tree"
        >
          <GitBranchPlus className="h-3.5 w-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-2" align="start">
        <p className="text-xs font-medium text-muted-foreground mb-1.5 px-2">Task Tree</p>
        {isLoading ? (
          <div className="flex items-center justify-center py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        ) : data ? (
          <div className="max-h-64 overflow-y-auto">
            {renderNode(data as TaskTreeNode, 0)}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground text-center py-2">No tree data</p>
        )}
      </PopoverContent>
    </Popover>
  )
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function CommentItem({ comment }: { comment: TaskCommentPublic }) {
  const isSystem = ["status_change", "assignment", "system"].includes(comment.comment_type)
  const isAgent = !!comment.author_agent_id
  const attachments = (comment.inline_attachments as TaskAttachmentPublic[] | undefined) ?? []

  if (isSystem) {
    return (
      <div className="flex items-start gap-2 py-1.5">
        <div className="w-5 h-5 rounded-full bg-muted flex items-center justify-center shrink-0 mt-0.5">
          <div className="w-1.5 h-1.5 rounded-full bg-muted-foreground/40" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-xs text-muted-foreground">{comment.content}</p>
        </div>
        <RelativeTime timestamp={comment.created_at} className="text-xs text-muted-foreground shrink-0" />
      </div>
    )
  }

  return (
    <div className="flex items-start gap-2.5 py-2">
      <div className={cn(
        "w-7 h-7 rounded-full flex items-center justify-center shrink-0 mt-0.5",
        isAgent ? "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300" : "bg-muted text-muted-foreground"
      )}>
        {isAgent ? <Bot className="h-3.5 w-3.5" /> : <User className="h-3.5 w-3.5" />}
      </div>
      <div className="flex-1 min-w-0 space-y-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium">{comment.author_name || (isAgent ? "Agent" : "User")}</span>
          {comment.author_role && <span className="text-xs text-muted-foreground">({comment.author_role})</span>}
          <RelativeTime timestamp={comment.created_at} className="text-xs text-muted-foreground" />
        </div>
        <div className="text-sm prose prose-sm dark:prose-invert max-w-none">
          <MarkdownRenderer content={comment.content} />
        </div>
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-1.5 pt-1">
            {attachments.map((att) => <AttachmentChip key={att.id} attachment={att} />)}
          </div>
        )}
      </div>
    </div>
  )
}

function AttachmentChip({ attachment }: { attachment: TaskAttachmentPublic }) {
  const handleDownload = async () => {
    if (!attachment.download_url) return
    try {
      await downloadAuthenticatedFile(
        attachment.download_url,
        attachment.file_name || "download",
      )
    } catch (error) {
      console.error("Failed to download attachment:", error)
    }
  }

  return (
    <button
      onClick={handleDownload}
      className="inline-flex items-center gap-1.5 px-2 py-1 rounded border text-xs hover:bg-muted transition-colors"
    >
      <FileText className="h-3 w-3 text-muted-foreground" />
      <span className="truncate max-w-[160px]">{attachment.file_name}</span>
      {attachment.file_size && <span className="text-muted-foreground shrink-0">({formatFileSize(attachment.file_size)})</span>}
      <Download className="h-3 w-3 text-muted-foreground shrink-0" />
    </button>
  )
}

function SubtaskRow({ subtask }: { subtask: InputTaskDetailPublic }) {
  const navigate = useNavigate()
  return (
    <button
      onClick={() => navigate({ to: "/task/$taskId", params: { taskId: subtask.id } })}
      className="w-full flex items-center gap-2 px-3 py-2 rounded-md hover:bg-muted/60 transition-colors text-left"
    >
      <span className="shrink-0" title={subtask.status}>
        {treeStatusIcons[subtask.status] ?? <Circle className="h-3 w-3 text-gray-500" />}
      </span>
      {subtask.short_code && <span className="font-mono text-xs text-muted-foreground shrink-0">{subtask.short_code}</span>}
      <span className="text-sm truncate flex-1">{subtask.title || subtask.current_description}</span>
      {subtask.agent_name && (
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground shrink-0">
          <Bot className="h-3 w-3" />{subtask.agent_name}
        </span>
      )}
      <RelativeTime timestamp={subtask.updated_at} className="text-xs text-muted-foreground shrink-0" showTooltip />
    </button>
  )
}

function SessionRunItem({ session, agentName, agentColorPreset }: { session: SessionPublic; agentName?: string | null; agentColorPreset?: ReturnType<typeof getColorPreset> | null }) {
  const navigate = useNavigate()
  const isActive = session.interaction_status === "running" || session.interaction_status === "pending_stream"

  return (
    <button
      onClick={() => navigate({
        to: "/session/$sessionId",
        params: { sessionId: session.id },
        search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined, pageContext: undefined },
      })}
      className="w-full flex items-center gap-2 px-3 py-2 rounded-md hover:bg-muted/60 transition-colors text-left"
    >
      <span className={cn("w-2 h-2 rounded-full shrink-0", isActive ? "bg-blue-500 animate-pulse" : session.status === "completed" ? "bg-green-500" : "bg-muted-foreground")} />
      {agentName && (
        <span className={cn(
          "inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs shrink-0",
          agentColorPreset ? `${agentColorPreset.badgeBg} ${agentColorPreset.badgeText}` : "bg-muted text-muted-foreground"
        )}>
          <Bot className="h-3 w-3" />{agentName}
        </span>
      )}
      <span className="text-sm truncate flex-1">{session.title || "Session"}</span>
      {isActive && (
        <span className="text-xs text-blue-500 shrink-0">Running</span>
      )}
      <RelativeTime timestamp={session.updated_at} className="text-xs text-muted-foreground shrink-0" />
    </button>
  )
}

// ---------------------------------------------------------------------------
// Header component — extracted to avoid inline JSX identity churn in useEffect
// ---------------------------------------------------------------------------

interface TaskPageHeaderProps {
  task: InputTaskDetailPublic
  displayTitle: string
  triggersCount: number
  headerMenuOpen: boolean
  onBack: () => void
  onMenuOpenChange: (open: boolean) => void
  onEditDescription: () => void
  onOpenTriggers: () => void
  onSendEmailReply: () => void
  onArchive: () => void
  onDelete: () => void
}

function TaskPageHeader({
  task, displayTitle, triggersCount, headerMenuOpen,
  onBack, onMenuOpenChange, onEditDescription, onOpenTriggers, onSendEmailReply, onArchive, onDelete,
}: TaskPageHeaderProps) {
  return (
    <>
      <div className="flex items-center gap-3 min-w-0">
        <Button variant="ghost" size="sm" onClick={onBack} className="shrink-0">
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="min-w-0">
          <h1 className="text-base font-semibold truncate">{displayTitle}</h1>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            {task.short_code && <TaskShortCodeBadge shortCode={task.short_code} status={task.status} clickable={false} />}
            {task.team_name && (
              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-muted text-muted-foreground">{task.team_name}</span>
            )}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <DropdownMenu open={headerMenuOpen} onOpenChange={onMenuOpenChange}>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" className="shrink-0">
              <EllipsisVertical className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={onEditDescription}>
              <Edit className="h-4 w-4 mr-2" />Edit Description
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onOpenTriggers}>
              <Zap className="h-4 w-4 mr-2" />Triggers
              {triggersCount > 0 && (
                <span className="ml-auto bg-primary/10 text-primary rounded-full px-1.5 py-0.5 text-xs font-medium">{triggersCount}</span>
              )}
            </DropdownMenuItem>
            {task.source_email_message_id && ["completed", "error"].includes(task.status) && (
              <DropdownMenuItem onClick={onSendEmailReply}>
                <Mail className="h-4 w-4 mr-2" />Send Email Reply
              </DropdownMenuItem>
            )}
            {task.status !== "archived" && (
              <DropdownMenuItem onClick={onArchive}>
                <Archive className="h-4 w-4 mr-2" />Archive Task
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={onDelete} className="text-destructive focus:text-destructive">
              <Trash2 className="h-4 w-4 mr-2" />Delete Task
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

function TaskDetailPage() {
  const { taskId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const { workspaceFilter } = useWorkspace()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { goBack } = useNavigationHistory()
  const isByUUID = isUUID(taskId)

  const [isEditing, setIsEditing] = useState(false)
  const [editedDescription, setEditedDescription] = useState("")
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [agentSelectorOpen, setAgentSelectorOpen] = useState(false)
  const [inputText, setInputText] = useState("")
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false)
  const [selectedText, setSelectedText] = useState<string | null>(null)
  const [sessionsModalOpen, setSessionsModalOpen] = useState(false)
  const [triggerModalOpen, setTriggerModalOpen] = useState(false)
  const [isDraggingOver, setIsDraggingOver] = useState(false)
  const [activeTab, setActiveTab] = useState<BodyTab>("comments")
  const [priorityMenuOpen, setPriorityMenuOpen] = useState(false)
  const [teamSelectorOpen, setTeamSelectorOpen] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const taskBodyRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // ---------------------------------------------------------------------------
  // Queries
  // ---------------------------------------------------------------------------

  const { data: task, isLoading, error } = useQuery({
    queryKey: ["task-detail", taskId],
    queryFn: () => isByUUID ? TasksService.getTaskDetail({ id: taskId }) : TasksService.getTaskDetailByCode({ shortCode: taskId }),
  })

  const { data: agentsData } = useQuery({
    queryKey: ["agents", workspaceFilter],
    queryFn: ({ queryKey }) => AgentsService.readAgents({ skip: 0, limit: 100, userWorkspaceId: queryKey[1] as string | undefined }),
  })

  const { data: sessionsData } = useQuery({
    queryKey: ["task-sessions", task?.id],
    queryFn: () => TasksService.listTaskSessions({ id: task!.id }),
    enabled: !!task?.id,
  })

  const { data: triggersData } = useQuery({
    queryKey: ["task-triggers", task?.id],
    queryFn: () => TaskTriggersApi.listTriggers(task!.id),
    enabled: !!task?.id,
  })

  const { data: teamsData } = useQuery({
    queryKey: ["agentic-teams"],
    queryFn: () => AgenticTeamsService.listAgenticTeams({ limit: 100 }),
  })

  // ---------------------------------------------------------------------------
  // Mutations
  // ---------------------------------------------------------------------------

  const updateMutation = useMutation({
    mutationFn: (data: { current_description?: string; selected_agent_id?: string; title?: string; priority?: string; team_id?: string | null; assigned_node_id?: string | null }) =>
      TasksService.updateTask({ id: task!.id, requestBody: data }),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] })
      if (variables.current_description !== undefined) { setIsEditing(false); showSuccessToast("Task updated") }
    },
    onError: (err) => showErrorToast((err as Error).message || "Failed to update task"),
  })

  const executeMutation = useMutation({
    mutationFn: () => TasksService.executeTask({ id: task!.id, requestBody: { mode: "conversation" } }),
    onSuccess: (result) => {
      if (result.success) {
        queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] })
        queryClient.invalidateQueries({ queryKey: ["task-sessions", task!.id] })
        showSuccessToast("Task execution started")
      } else showErrorToast(result.error || "Failed to execute task")
    },
    onError: (err) => showErrorToast((err as Error).message || "Failed to execute task"),
  })

  const deleteMutation = useMutation({
    mutationFn: () => TasksService.deleteTask({ id: task!.id }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["tasks"] }); navigate({ to: "/tasks" }); showSuccessToast("Task deleted") },
    onError: (err) => showErrorToast((err as Error).message || "Failed to delete task"),
  })

  const archiveMutation = useMutation({
    mutationFn: () => TasksService.archiveTask({ id: task!.id }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] }); queryClient.invalidateQueries({ queryKey: ["tasks"] }); showSuccessToast("Task archived") },
    onError: (err) => showErrorToast((err as Error).message || "Failed to archive task"),
  })

  const refineMutation = useMutation({
    mutationFn: (params: { userComment: string; userSelectedText: string | null }) =>
      TasksService.refineTask({ id: task!.id, requestBody: { user_comment: params.userComment, user_selected_text: params.userSelectedText } }),
    onSuccess: (result) => {
      if (result.success && result.refined_description) {
        queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] })
        setEditedDescription(result.refined_description)
      }
      setInputText(""); setSelectedText(null); window.getSelection()?.removeAllRanges()
    },
    onError: (err) => showErrorToast((err as Error).message || "Failed to refine task"),
  })

  const autoRefineMutation = useMutation({
    mutationFn: () => TasksService.refineTask({ id: task!.id, requestBody: { user_comment: "Please analyze and refine this task automatically." } }),
    onSuccess: (result) => {
      if (result.success && result.refined_description) {
        queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] })
        setEditedDescription(result.refined_description)
      }
    },
    onError: (err) => showErrorToast((err as Error).message || "Failed to auto-refine task"),
  })

  const addCommentMutation = useMutation({
    mutationFn: (content: string) => TasksService.addTaskComment({ id: task!.id, requestBody: { content } }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] }); setInputText(""); showSuccessToast("Comment added") },
    onError: () => showErrorToast("Failed to add comment"),
  })

  const sendAnswerMutation = useMutation({
    mutationFn: () => TasksService.sendTaskEmailAnswer({ id: task!.id, requestBody: {} }),
    onSuccess: (data) => { data.success ? showSuccessToast("Email reply queued") : showErrorToast(data.error || "Failed") },
    onError: (err) => showErrorToast((err as Error).message || "Failed to send email reply"),
  })

  const uploadAttachmentMutation = useMutation({
    mutationFn: (file: File) => TasksService.uploadTaskAttachment({ id: task!.id, formData: { file } }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] }); showSuccessToast("File attached") },
    onError: () => showErrorToast("Failed to upload file"),
  })

  // ---------------------------------------------------------------------------
  // Derived data
  // ---------------------------------------------------------------------------

  const agents = agentsData?.data || []
  const sessions = (sessionsData?.data || []) as SessionPublic[]
  const comments = (task?.comments as TaskCommentPublic[] | undefined) ?? []
  const userComments = comments.filter((c) => !["status_change", "assignment", "system"].includes(c.comment_type))
  const systemComments = comments.filter((c) => ["status_change", "assignment", "system"].includes(c.comment_type))
  const attachments = (task?.attachments as TaskAttachmentPublic[] | undefined) ?? []
  const subtasks = (task?.subtasks as InputTaskDetailPublic[] | undefined) ?? []
  const selectedAgent = agents.find((a) => a.id === task?.selected_agent_id)
  const agentColorPreset = selectedAgent ? getColorPreset(selectedAgent.ui_color_preset) : null
  const canExecute = task?.selected_agent_id && !["running", "archived"].includes(task?.status ?? "")

  // ---------------------------------------------------------------------------
  // Real-time events
  // ---------------------------------------------------------------------------

  const handleTaskEvent = useCallback(
    (event: { meta?: { task_id?: string; short_code?: string; parent_task_id?: string; parent_short_code?: string } }) => {
      const meta = event.meta
      if (!meta || !task) return
      const isThisTask =
        meta.task_id === task.id ||
        (meta.short_code && meta.short_code === task.short_code) ||
        meta.parent_task_id === task.id ||
        (meta.parent_short_code && meta.parent_short_code === task.short_code)
      if (isThisTask) {
        queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] })
        queryClient.invalidateQueries({ queryKey: ["task-sessions", task.id] })
      }
    },
    [task?.id, task?.short_code, taskId, queryClient],
  )

  useMultiEventSubscription(
    [EventTypes.TASK_COMMENT_ADDED, EventTypes.TASK_STATUS_CHANGED, EventTypes.TASK_ATTACHMENT_ADDED, EventTypes.SUBTASK_COMPLETED, EventTypes.TASK_SUBTASK_CREATED],
    handleTaskEvent,
  )

  // Keep a ref of current session IDs so the WS handler always sees the latest list
  const sessionIdsRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    sessionIdsRef.current = new Set(sessions.map((s) => s.id))
  }, [sessions])

  // Refresh sessions block when any session updates (status, streaming, completion)
  const handleSessionEvent = useCallback(
    (event: { model_id?: string; meta?: { source_task_id?: string; session_id?: string } }) => {
      if (!task) return
      const meta = event.meta
      const eventSessionId = meta?.session_id || event.model_id
      // Refresh if the event is for a session belonging to this task
      if (meta?.source_task_id === task.id || (eventSessionId && sessionIdsRef.current.has(eventSessionId))) {
        queryClient.invalidateQueries({ queryKey: ["task-sessions", task.id] })
        queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] })
      }
    },
    [task?.id, taskId, queryClient],
  )

  useMultiEventSubscription(
    [EventTypes.SESSION_UPDATED, EventTypes.SESSION_INTERACTION_STATUS_CHANGED, EventTypes.SESSION_STATE_UPDATED, EventTypes.STREAM_COMPLETED],
    handleSessionEvent,
  )

  // ---------------------------------------------------------------------------
  // Header
  // ---------------------------------------------------------------------------

  const sendAnswerMutateRef = useRef(sendAnswerMutation.mutate)
  sendAnswerMutateRef.current = sendAnswerMutation.mutate

  useEffect(() => {
    if (!task) return
    const displayTitle = task.title || task.current_description?.slice(0, 80) || "Task"
    setHeaderContent(
      <TaskPageHeader
        task={task}
        displayTitle={displayTitle}
        triggersCount={triggersData?.count ?? 0}
        headerMenuOpen={headerMenuOpen}
        onBack={() => goBack("/tasks")}
        onMenuOpenChange={setHeaderMenuOpen}
        onEditDescription={() => { setHeaderMenuOpen(false); setIsEditing((v) => !v) }}
        onOpenTriggers={() => { setHeaderMenuOpen(false); setTriggerModalOpen(true) }}
        onSendEmailReply={() => { setHeaderMenuOpen(false); sendAnswerMutateRef.current() }}
        onArchive={() => { setHeaderMenuOpen(false); archiveMutation.mutate() }}
        onDelete={() => { setHeaderMenuOpen(false); setDeleteDialogOpen(true) }}
      />
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, task, navigate, headerMenuOpen, isEditing, triggersData?.count])

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  useEffect(() => { if (task) setEditedDescription(task.current_description) }, [task?.current_description])
  useEffect(() => { setSelectedText(null) }, [task?.current_description])

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => { e.preventDefault(); e.stopPropagation(); setIsDraggingOver(true) }, [])
  const handleDragLeave = useCallback((e: DragEvent<HTMLDivElement>) => { e.preventDefault(); e.stopPropagation(); if (e.currentTarget === e.target) setIsDraggingOver(false) }, [])
  const handleDrop = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault(); e.stopPropagation(); setIsDraggingOver(false)
    for (const file of Array.from(e.dataTransfer.files)) {
      if (file.size > 100 * 1024 * 1024) { showErrorToast(`File ${file.name} too large (max 100MB)`); continue }
      uploadAttachmentMutation.mutate(file)
    }
  }, [uploadAttachmentMutation, showErrorToast])

  const handleTextSelection = useCallback(() => {
    if (isEditing) return
    const selection = window.getSelection()
    if (!selection || selection.isCollapsed) return
    const text = selection.toString().trim()
    if (text && taskBodyRef.current?.contains(selection.anchorNode)) {
      setSelectedText(text)
      setTimeout(() => inputRef.current?.focus(), 0)
    }
  }, [isEditing])

  const handleContainerClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as HTMLElement
    if (taskBodyRef.current?.contains(target) || inputRef.current?.contains(target) || target.closest("[data-selected-text-indicator]")) return
    setSelectedText(null); window.getSelection()?.removeAllRanges()
  }, [])

  const handleSaveDescription = () => {
    if (editedDescription !== task?.current_description) updateMutation.mutate({ current_description: editedDescription })
    else setIsEditing(false)
  }

  const handleAgentSelect = (agentId: string) => {
    updateMutation.mutate({ selected_agent_id: agentId || undefined })
    setAgentSelectorOpen(false)
    setTimeout(() => inputRef.current?.focus(), 100)
  }

  const handleTeamChange = async (teamId: string | null) => {
    if (teamId) {
      const nodesResp = await AgenticTeamsService.listTeamNodes({ teamId })
      const leadNode = (nodesResp.data as AgenticTeamNodePublic[]).find((n) => n.is_lead)
      updateMutation.mutate({
        team_id: teamId,
        selected_agent_id: leadNode?.agent_id ?? undefined,
        assigned_node_id: leadNode?.id ?? undefined,
      })
    } else {
      updateMutation.mutate({ team_id: null, assigned_node_id: null })
    }
  }

  const handleRefineSubmit = () => {
    if (!inputText.trim() || refineMutation.isPending) return
    refineMutation.mutate({ userComment: inputText.trim(), userSelectedText: selectedText })
  }

  const handleCommentSubmit = () => {
    if (!inputText.trim() || addCommentMutation.isPending) return
    addCommentMutation.mutate(inputText.trim())
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleCommentSubmit() }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleRefineSubmit() }
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) { uploadAttachmentMutation.mutate(file); e.target.value = "" }
  }

  // ---------------------------------------------------------------------------
  // Loading / error
  // ---------------------------------------------------------------------------

  if (isLoading) return <PendingItems />
  if (error || !task) {
    const isMissing = !error || (error as { status?: number }).status === 404
    if (isMissing) {
      return (
        <NotFound
          inline
          fallbackPath="/tasks"
          title="Task not found"
          message="This task doesn't exist, was deleted, or belongs to another user."
        />
      )
    }
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading task</p>
        <Button variant="outline" onClick={() => goBack("/tasks")} className="mt-4">Back to Tasks</Button>
      </div>
    )
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  const priorityConfig: Record<string, { label: string; color: string }> = {
    low: { label: "Low", color: "text-gray-500" },
    normal: { label: "Normal", color: "text-muted-foreground" },
    high: { label: "High", color: "text-orange-600 dark:text-orange-400" },
    urgent: { label: "Urgent", color: "text-red-600 dark:text-red-400" },
  }
  const currentPriority = task.priority || "normal"

  return (
    <div
      className={cn("flex flex-col h-full transition-colors", isDraggingOver && "border-2 border-dashed border-primary bg-primary/5")}
      onClick={handleContainerClick}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {isDraggingOver && (
        <div className="absolute inset-0 flex items-center justify-center bg-background/80 z-50 pointer-events-none">
          <div className="text-lg font-medium text-primary">Drop files to attach</div>
        </div>
      )}

      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* ---- Left: main content area ---- */}
        <div className="flex-1 min-w-0 flex flex-col overflow-y-auto">
          {/* Description */}
          <div className="px-6 py-5 flex-shrink-0">
            <div>
              {isEditing ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-muted-foreground">Editing description</span>
                    <div className="flex items-center gap-2">
                      <Button variant="ghost" size="sm" onClick={() => { setEditedDescription(task.current_description); setIsEditing(false) }}>Cancel</Button>
                      <Button size="sm" onClick={handleSaveDescription} disabled={updateMutation.isPending}>
                        {updateMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "Save"}
                      </Button>
                    </div>
                  </div>
                  <Textarea value={editedDescription} onChange={(e) => setEditedDescription(e.target.value)} className="min-h-[200px] resize-none text-base" placeholder="Task description..." />
                </div>
              ) : (
                <div ref={taskBodyRef} onMouseUp={handleTextSelection} className="prose prose-base dark:prose-invert max-w-none selection:bg-amber-200 dark:selection:bg-amber-700">
                  <MarkdownRenderer content={task.current_description} className="text-sm leading-relaxed" />
                </div>
              )}
            </div>
          </div>

          {/* Attachments section */}
          <div className="px-6 pb-4 flex-shrink-0">
            <div>
              <div className="flex items-center gap-2 mb-2">
                <span className="text-sm font-medium">Attachments</span>
                <button
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploadAttachmentMutation.isPending}
                  className="text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
                  title="Attach file"
                >
                  {uploadAttachmentMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
                </button>
              </div>
              {attachments.length === 0 ? (
                <p className="text-xs text-muted-foreground">No attachments yet.</p>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {attachments.map((att) => <AttachmentChip key={att.id} attachment={att} />)}
                </div>
              )}
            </div>
          </div>

          {/* Tabs: Comments / Sessions / Sub-tasks / Activity */}
          <div className="border-t flex-shrink-0">
            <div className="px-6">
              <div className="flex gap-6 border-b">
                {([
                  { key: "comments" as BodyTab, label: "Comments", icon: MessageSquare, count: userComments.length },
                  { key: "sessions" as BodyTab, label: "Sessions", icon: Play, count: sessions.length },
                  { key: "subtasks" as BodyTab, label: "Sub-tasks", icon: ListTree, count: subtasks.length },
                  { key: "activity" as BodyTab, label: "Activity", icon: Activity, count: systemComments.length },
                ]).map((tab) => {
                  const hasActiveSession = tab.key === "sessions" && sessions.some((s) => s.interaction_status === "running" || s.interaction_status === "pending_stream" || s.interaction_status === "streaming")
                  const hasActiveSubtask = tab.key === "subtasks" && subtasks.some((s) => s.status === "in_progress")
                  const iconActive = hasActiveSession || hasActiveSubtask
                  return (
                    <button
                      key={tab.key}
                      onClick={() => setActiveTab(tab.key)}
                      className={cn(
                        "flex items-center gap-1.5 py-3 text-sm font-medium border-b-2 -mb-px transition-colors",
                        activeTab === tab.key ? "border-foreground text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"
                      )}
                    >
                      <tab.icon className={cn("h-3.5 w-3.5", iconActive && "text-blue-500 animate-pulse")} />
                      {tab.label}
                      {tab.count > 0 && <span className="inline-flex items-center justify-center h-5 min-w-5 px-1 rounded-full bg-muted text-[11px] font-medium text-muted-foreground">{tab.count}</span>}
                    </button>
                  )
                })}
              </div>
            </div>
          </div>

          {/* Tab content */}
          <div className="flex-1 px-6 py-4 overflow-y-auto">
            <div>
              {activeTab === "comments" && (
                <>
                  {userComments.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-4 text-center">No comments yet</p>
                  ) : (
                    <div className="space-y-0.5 divide-y divide-border/50">
                      {userComments.map((c) => <CommentItem key={c.id} comment={c} />)}
                    </div>
                  )}
                </>
              )}
              {activeTab === "sessions" && (
                <>
                  {sessions.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-4 text-center">No sessions yet</p>
                  ) : (
                    <div className="space-y-0.5">
                      {sessions.map((s) => (
                        <SessionRunItem key={s.id} session={s} agentName={task.agent_name} agentColorPreset={agentColorPreset} />
                      ))}
                    </div>
                  )}
                </>
              )}
              {activeTab === "subtasks" && (
                <>
                  {subtasks.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-4 text-center">No sub-issues</p>
                  ) : (
                    <div className="space-y-0.5">
                      {subtasks.map((sub) => <SubtaskRow key={sub.id} subtask={sub} />)}
                    </div>
                  )}
                </>
              )}
              {activeTab === "activity" && (
                <>
                  {systemComments.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-4 text-center">No activity yet</p>
                  ) : (
                    <div className="space-y-0.5 divide-y divide-border/50">
                      {systemComments.map((c) => <CommentItem key={c.id} comment={c} />)}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Selected text indicator */}
          {selectedText && (
            <div className="px-6 pb-2 flex-shrink-0" data-selected-text-indicator>
              <div className="flex items-center gap-2 bg-amber-100 dark:bg-amber-900/70 text-amber-800 dark:text-amber-200 rounded-lg px-3 py-2 border border-amber-300 dark:border-amber-700">
                <TextSelect className="h-4 w-4 shrink-0" />
                <span className="text-xs truncate max-w-md italic">"{selectedText.length > 100 ? selectedText.slice(0, 100) + "..." : selectedText}"</span>
                <button onClick={() => { setSelectedText(null); window.getSelection()?.removeAllRanges() }} className="shrink-0 p-0.5 rounded hover:bg-amber-200 dark:hover:bg-amber-800 transition-colors">
                  <X className="h-3 w-3" />
                </button>
              </div>
            </div>
          )}

          {/* Input area — inline like session message input */}
          <div className="border-t px-6 py-3 flex-shrink-0 bg-background/60">
            <div className="flex items-center gap-2">
              {/* Attach button */}
              <Button
                variant="outline"
                size="icon"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploadAttachmentMutation.isPending}
                title="Attach file"
                className="h-10 w-10 shrink-0 rounded-lg"
              >
                {uploadAttachmentMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
              </Button>
              {/* Text input */}
              <Textarea
                ref={inputRef}
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={selectedText ? "Describe how to refine the selected text..." : "Add a comment or refinement..."}
                rows={1}
                className="resize-none text-sm flex-1 min-h-[40px] max-h-[120px] py-2"
                disabled={refineMutation.isPending || addCommentMutation.isPending}
              />
              {/* Refine button */}
              <Button
                variant="outline"
                size="icon"
                onClick={inputText.trim() ? handleRefineSubmit : () => autoRefineMutation.mutate()}
                disabled={refineMutation.isPending || autoRefineMutation.isPending}
                title={inputText.trim() ? "Refine with instructions (Ctrl+Enter)" : "Auto-refine"}
                className="h-10 w-10 shrink-0 rounded-lg hover:text-amber-500 hover:border-amber-500/50 hover:bg-amber-500/10 transition-colors"
              >
                {refineMutation.isPending || autoRefineMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin text-amber-500" /> : <Sparkles className="h-4 w-4 text-amber-500" />}
              </Button>
              {/* Send/Comment button */}
              <Button
                size="icon"
                onClick={handleCommentSubmit}
                disabled={!inputText.trim() || addCommentMutation.isPending}
                title="Send comment (Enter)"
                className="h-10 w-10 shrink-0 rounded-lg"
              >
                {addCommentMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              </Button>
            </div>
            <input ref={fileInputRef} type="file" className="hidden" onChange={handleFileChange} />
          </div>
        </div>

        {/* ---- Right: Properties panel ---- */}
        <div className="w-72 border-l bg-card/30 flex-shrink-0 overflow-y-auto hidden lg:flex lg:flex-col">
          <div className="px-5 py-4 space-y-4 flex-1">
            {/* Parent Task */}
            {task.parent_task_id && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground font-medium flex items-center gap-1">
                  Parent Task
                  {(task.root_short_code || task.parent_short_code) && (
                    <TaskTreePopover
                      rootShortCode={task.root_short_code || task.parent_short_code!}
                      currentTaskId={task.id}
                    />
                  )}
                </span>
                <button
                  onClick={() => navigate({ to: "/task/$taskId", params: { taskId: task.parent_short_code || task.parent_task_id! } })}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-mono font-medium bg-muted hover:bg-muted/80 text-foreground transition-colors"
                >
                  {task.parent_short_code || "Parent"}
                </button>
              </div>
            )}

            {/* Status */}
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground font-medium w-20">Status</span>
              <TaskStatusPill status={task.status} />
            </div>

            {/* Priority — dropdown */}
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground font-medium w-20">Priority</span>
              <DropdownMenu open={priorityMenuOpen} onOpenChange={setPriorityMenuOpen}>
                <DropdownMenuTrigger asChild>
                  <button className={cn("text-sm font-medium flex items-center gap-1 hover:underline", priorityConfig[currentPriority]?.color)}>
                    {currentPriority === "urgent" && <span className="w-2 h-2 rounded-full bg-red-500 shrink-0" />}
                    {currentPriority === "high" && <span className="w-2 h-2 rounded-full bg-orange-500 shrink-0" />}
                    {priorityConfig[currentPriority]?.label || currentPriority}
                    <ChevronDown className="h-3 w-3" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {Object.entries(priorityConfig).map(([key, cfg]) => (
                    <DropdownMenuItem key={key} onClick={() => { updateMutation.mutate({ priority: key }); setPriorityMenuOpen(false) }} className={cn(task.priority === key && "font-semibold")}>
                      {key === "urgent" && <span className="w-2 h-2 rounded-full bg-red-500 mr-2" />}
                      {key === "high" && <span className="w-2 h-2 rounded-full bg-orange-500 mr-2" />}
                      {key === "normal" && <span className="w-2 h-2 rounded-full bg-gray-400 mr-2" />}
                      {key === "low" && <span className="w-2 h-2 rounded-full bg-gray-300 mr-2" />}
                      {cfg.label}
                      {task.priority === key && <Check className="h-3 w-3 ml-auto" />}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>

            {/* Assignee */}
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground font-medium w-20">Assignee</span>
              <button
                onClick={() => setAgentSelectorOpen(true)}
                className={cn(
                  "flex items-center gap-1.5 px-2 py-0.5 rounded-md text-sm transition-all",
                  agentColorPreset ? `${agentColorPreset.badgeBg} ${agentColorPreset.badgeText} ${agentColorPreset.badgeHover}` : "bg-muted text-muted-foreground hover:bg-muted/80"
                )}
              >
                <Bot className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate max-w-[120px]">{selectedAgent?.name || "Unassigned"}</span>
              </button>
            </div>

            {/* Team */}
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground font-medium w-20">Team</span>
              {(() => {
                const currentTeam = (teamsData?.data || []).find((t) => t.id === task.team_id)
                const TeamIcon = currentTeam ? getWorkspaceIcon(currentTeam.icon) : null
                return (
                  <button
                    onClick={() => setTeamSelectorOpen(true)}
                    className="flex items-center gap-1.5 px-2 py-0.5 rounded-md text-sm bg-muted text-foreground hover:bg-muted/80 transition-all"
                  >
                    {TeamIcon && <TeamIcon className="h-3.5 w-3.5 shrink-0" />}
                    <span className="truncate max-w-[120px]">{task.team_name || "None"}</span>
                  </button>
                )
              })()}
            </div>


            {/* Triggers */}
            {(triggersData?.count ?? 0) > 0 && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground font-medium w-20">Triggers</span>
                <button onClick={() => setTriggerModalOpen(true)} className="text-sm hover:underline flex items-center gap-1">
                  <Zap className="h-3 w-3" />{triggersData?.count}
                </button>
              </div>
            )}

            {/* Subtask progress */}
            {(task.subtask_count ?? 0) > 0 && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground font-medium w-20 flex items-center gap-1">
                  Subtasks
                  {!task.parent_task_id && task.short_code && (
                    <TaskTreePopover
                      rootShortCode={task.short_code}
                      currentTaskId={task.id}
                    />
                  )}
                </span>
                <SubtaskProgressChip total={task.subtask_count ?? 0} completed={task.subtask_completed_count ?? 0} taskId={task.id} />
              </div>
            )}

            {/* Dates */}
            <div className="space-y-2 pt-3 border-t opacity-50">
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">Created</span>
                <RelativeTime timestamp={task.created_at} className="text-muted-foreground" />
              </div>
              {task.executed_at && (
                <div className="flex justify-between text-xs">
                  <span className="text-muted-foreground">Started</span>
                  <RelativeTime timestamp={task.executed_at} className="text-muted-foreground" />
                </div>
              )}
              {task.completed_at && (
                <div className="flex justify-between text-xs">
                  <span className="text-muted-foreground">Completed</span>
                  <RelativeTime timestamp={task.completed_at} className="text-muted-foreground" />
                </div>
              )}
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">Updated</span>
                <RelativeTime timestamp={task.updated_at} className="text-muted-foreground" />
              </div>
            </div>
          </div>

          {/* Execute at bottom of panel */}
          <div className="px-5 py-4 border-t space-y-2 flex-shrink-0">
            <Button className="w-full" onClick={() => executeMutation.mutate()} disabled={!canExecute || executeMutation.isPending}>
              {executeMutation.isPending ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> : <Play className="h-4 w-4 mr-1.5" />}
              {sessions.length > 0 ? "Run Again" : "Execute Task"}
            </Button>
            {task.source_email_message_id && ["completed", "error"].includes(task.status) && (
              <Button variant="outline" className="w-full" onClick={() => sendAnswerMutation.mutate()} disabled={sendAnswerMutation.isPending}>
                {sendAnswerMutation.isPending ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> : <Mail className="h-4 w-4 mr-1.5" />}
                Send Email Reply
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* ---- Modals ---- */}
      <AgentSelectorDialog
        open={agentSelectorOpen}
        onOpenChange={setAgentSelectorOpen}
        onSelect={handleAgentSelect}
        selectedAgentId={task.selected_agent_id}
        workspaceId={workspaceFilter ?? null}
      />

      <Dialog open={teamSelectorOpen} onOpenChange={setTeamSelectorOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader><DialogTitle>Select Team</DialogTitle></DialogHeader>
          <div className="flex flex-wrap gap-2 pt-4">
            <button
              className={cn("cursor-pointer px-4 py-2 text-sm rounded-md transition-all flex items-center gap-2 bg-muted text-muted-foreground hover:bg-muted/80", !task.team_id && "ring-2 ring-foreground/30")}
              onClick={() => { handleTeamChange(null); setTeamSelectorOpen(false) }}
            >
              <X className="h-4 w-4" />
            </button>
            {(teamsData?.data || []).map((team) => {
              const isSelected = task.team_id === team.id
              const Icon = getWorkspaceIcon(team.icon)
              return (
                <button
                  key={team.id}
                  className={cn("cursor-pointer px-4 py-2 text-sm rounded-md transition-all flex items-center gap-2 bg-muted text-foreground hover:bg-muted/80", isSelected && "ring-2 ring-foreground/30")}
                  onClick={() => { handleTeamChange(team.id); setTeamSelectorOpen(false) }}
                >
                  <Icon className="h-4 w-4" />
                  {team.name}
                </button>
              )
            })}
            {(teamsData?.data || []).length === 0 && <p className="text-sm text-muted-foreground">No teams available</p>}
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Task</AlertDialogTitle>
            <AlertDialogDescription>Are you sure? This cannot be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => deleteMutation.mutate()} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
              {deleteMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <TaskSessionsModal taskId={task.id} open={sessionsModalOpen} onOpenChange={setSessionsModalOpen} />
      <TriggerManagementModal taskId={task.id} open={triggerModalOpen} onOpenChange={setTriggerModalOpen} />
    </div>
  )
}
