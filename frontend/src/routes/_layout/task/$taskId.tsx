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
  Bot,
  X,
  TextSelect,
  Sparkles,
  Layers,
  ExternalLink,
  Plus,
  Zap,
  Mail,
} from "lucide-react"

import { TasksService, AgentsService, FilesService } from "@/client"
import type { FileUploadPublic } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import type { RefinementHistoryItem } from "@/components/Tasks/RefinementChat"
import { TaskSessionsModal } from "@/components/Tasks/TaskSessionsModal"
import { TriggerManagementModal } from "@/components/Tasks/Triggers/TriggerManagementModal"
import { TaskTriggersApi } from "@/components/Tasks/Triggers/triggerApi"
import { FileUploadModal } from "@/components/Chat/FileUploadModal"
import { FileBadge } from "@/components/Chat/FileBadge"
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
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useWorkspace from "@/hooks/useWorkspace"
import useCustomToast from "@/hooks/useCustomToast"
import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { getColorPreset } from "@/utils/colorPresets"
import { cn } from "@/lib/utils"

export const Route = createFileRoute("/_layout/task/$taskId")({
  component: TaskDetail,
})

function TaskDetail() {
  const { taskId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [isEditing, setIsEditing] = useState(false)
  const [editedDescription, setEditedDescription] = useState("")
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [agentSelectorOpen, setAgentSelectorOpen] = useState(false)
  const [refinementComment, setRefinementComment] = useState("")
  const [chatHistoryOpen, setChatHistoryOpen] = useState(false)
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false)
  const [selectedText, setSelectedText] = useState<string | null>(null)
  const [sessionsModalOpen, setSessionsModalOpen] = useState(false)
  const [triggerModalOpen, setTriggerModalOpen] = useState(false)
  const [showFileModal, setShowFileModal] = useState(false)
  const [isDraggingOver, setIsDraggingOver] = useState(false)
  const [attachedFiles, setAttachedFiles] = useState<FileUploadPublic[]>([])
  const refinementInputRef = useRef<HTMLTextAreaElement>(null)
  const taskBodyRef = useRef<HTMLDivElement>(null)

  const {
    data: task,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => TasksService.getTask({ id: taskId }),
  })

  const { data: agentsData } = useQuery({
    queryKey: ["agents", activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId ?? "",
      })
    },
  })

  // Fetch sessions spawned by this task
  const { data: sessionsData } = useQuery({
    queryKey: ["task-sessions", taskId],
    queryFn: () => TasksService.listTaskSessions({ id: taskId }),
  })

  // Fetch trigger count for badge display
  const { data: triggersData } = useQuery({
    queryKey: ["task-triggers", taskId],
    queryFn: () => TaskTriggersApi.listTriggers(taskId),
  })

  const updateMutation = useMutation({
    mutationFn: (data: { current_description?: string; selected_agent_id?: string }) =>
      TasksService.updateTask({ id: taskId, requestBody: data }),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ["task", taskId] })
      // Only show toast and exit editing mode for description changes
      if (variables.current_description !== undefined) {
        setIsEditing(false)
        showSuccessToast("Task updated")
      }
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to update task")
    },
  })

  const executeMutation = useMutation({
    mutationFn: () =>
      TasksService.executeTask({ id: taskId, requestBody: { mode: "conversation" } }),
    onSuccess: (result) => {
      if (result.success && result.session_id) {
        queryClient.invalidateQueries({ queryKey: ["task", taskId] })
        queryClient.invalidateQueries({ queryKey: ["task-sessions", taskId] })
        navigate({
          to: "/session/$sessionId",
          params: { sessionId: result.session_id },
          search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined },
        })
      } else {
        showErrorToast(result.error || "Failed to execute task")
      }
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to execute task")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => TasksService.deleteTask({ id: taskId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] })
      navigate({ to: "/tasks" })
      showSuccessToast("Task deleted")
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to delete task")
    },
  })

  const refineMutation = useMutation({
    mutationFn: (params: { userComment: string; userSelectedText: string | null }) =>
      TasksService.refineTask({
        id: taskId,
        requestBody: {
          user_comment: params.userComment,
          user_selected_text: params.userSelectedText,
        },
      }),
    onSuccess: (result) => {
      if (result.success && result.refined_description) {
        queryClient.invalidateQueries({ queryKey: ["task", taskId] })
        setEditedDescription(result.refined_description)
      }
      setRefinementComment("")
      setSelectedText(null)
      window.getSelection()?.removeAllRanges()
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to refine task")
    },
  })

  const autoRefineMutation = useMutation({
    mutationFn: () =>
      TasksService.refineTask({
        id: taskId,
        requestBody: { user_comment: "Please analyze and refine this task automatically." },
      }),
    onSuccess: (result) => {
      if (result.success && result.refined_description) {
        queryClient.invalidateQueries({ queryKey: ["task", taskId] })
        setEditedDescription(result.refined_description)
      }
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to auto-refine task")
    },
  })

  const sendAnswerMutation = useMutation({
    mutationFn: () =>
      TasksService.sendTaskEmailAnswer({
        id: taskId,
        requestBody: {},
      }),
    onSuccess: (data) => {
      if (data.success) {
        showSuccessToast("Email reply queued for delivery")
      } else {
        showErrorToast(data.error || "Failed to send email reply")
      }
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to send email reply")
    },
  })

  // File upload mutation
  const uploadFileMutation = useMutation({
    mutationFn: (file: File) => FilesService.uploadFile({ formData: { file } }),
    onSuccess: (uploadedFile) => {
      // After upload, attach to task
      attachFileMutation.mutate(uploadedFile.id)
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to upload file")
    },
  })

  // Attach file to task mutation
  const attachFileMutation = useMutation({
    mutationFn: (fileId: string) =>
      TasksService.attachFileToTask({ id: taskId, fileId }),
    onSuccess: (file) => {
      setAttachedFiles((prev) => [...prev, file])
      queryClient.invalidateQueries({ queryKey: ["task", taskId] })
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to attach file")
    },
  })

  // Detach file from task mutation
  const detachFileMutation = useMutation({
    mutationFn: (fileId: string) =>
      TasksService.detachFileFromTask({ id: taskId, fileId }),
    onSuccess: (_, fileId) => {
      setAttachedFiles((prev) => prev.filter((f) => f.id !== fileId))
      queryClient.invalidateQueries({ queryKey: ["task", taskId] })
    },
    onError: (error) => {
      showErrorToast((error as Error).message || "Failed to remove file")
    },
  })

  // Get selected agent info for header
  const headerSelectedAgent = agentsData?.data?.find(a => a.id === task?.selected_agent_id)
  const headerAgentColorPreset = headerSelectedAgent ? getColorPreset(headerSelectedAgent.ui_color_preset) : null

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center gap-4 w-full">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => navigate({ to: "/tasks" })}
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="flex-1 min-w-0">
          <h1 className="text-lg font-semibold truncate">Task Refinement</h1>
          <p className="text-xs text-muted-foreground">
            Refine and prepare your task for execution
          </p>
        </div>
        {task && (
          <div className="flex items-center gap-2">
            {/* Edit Task button */}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setIsEditing(!isEditing)}
              className="h-8"
            >
              <Edit className="h-4 w-4 mr-1.5" />
              Edit Task
            </Button>
            {/* Triggers button */}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setTriggerModalOpen(true)}
              className="h-8"
            >
              <Zap className="h-4 w-4 mr-1.5" />
              Triggers
              {(triggersData?.count ?? 0) > 0 && (
                <span className="ml-1.5 bg-primary/10 text-primary rounded-full px-1.5 py-0.5 text-xs font-medium">
                  {triggersData?.count}
                </span>
              )}
            </Button>
            {/* Agent selector in header with robot icon */}
            <button
              onClick={() => setAgentSelectorOpen(true)}
              disabled={updateMutation.isPending}
              className={cn(
                "h-8 px-3 rounded-md text-sm font-medium transition-all flex items-center gap-1.5",
                headerAgentColorPreset
                  ? `${headerAgentColorPreset.badgeBg} ${headerAgentColorPreset.badgeText} ${headerAgentColorPreset.badgeHover}`
                  : "bg-orange-500/20 text-orange-600 dark:text-orange-400 hover:bg-orange-500/30 ring-2 ring-orange-500/50 animate-pulse"
              )}
            >
              <Bot className="h-4 w-4" />
              {headerSelectedAgent ? headerSelectedAgent.name : "Select Agent"}
            </button>
            {/* Dropdown menu */}
            <DropdownMenu open={headerMenuOpen} onOpenChange={setHeaderMenuOpen}>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon">
                  <EllipsisVertical className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onClick={() => {
                    setHeaderMenuOpen(false)
                    setDeleteDialogOpen(true)
                  }}
                  className="text-destructive focus:text-destructive"
                >
                  <Trash2 className="h-4 w-4 mr-2" />
                  Delete Task
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        )}
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, task, navigate, headerSelectedAgent, headerAgentColorPreset, headerMenuOpen, updateMutation.isPending, isEditing, triggersData])

  useEffect(() => {
    if (task) {
      setEditedDescription(task.current_description)
    }
  }, [task])

  // Clear selected text when task description changes
  useEffect(() => {
    setSelectedText(null)
  }, [task?.current_description])

  // Sync attached files from task data
  useEffect(() => {
    if (task?.attached_files) {
      setAttachedFiles(task.attached_files)
    }
  }, [task?.attached_files])

  // Drag-drop handlers for file upload
  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(true)
  }, [])

  const handleDragLeave = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    // Only set to false if we're leaving the container, not entering a child
    if (e.currentTarget === e.target) {
      setIsDraggingOver(false)
    }
  }, [])

  const handleDrop = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(false)

    const files = Array.from(e.dataTransfer.files)
    for (const file of files) {
      // Validate file size (100MB)
      if (file.size > 100 * 1024 * 1024) {
        showErrorToast(`File ${file.name} is too large (max 100MB)`)
        continue
      }
      uploadFileMutation.mutate(file)
    }
  }, [uploadFileMutation, showErrorToast])

  // Handle file upload from modal
  const handleFileUploaded = useCallback((file: FileUploadPublic) => {
    // File is already uploaded, just attach it to the task
    attachFileMutation.mutate(file.id)
    setShowFileModal(false)
  }, [attachFileMutation])

  // Handle text selection in the task body
  const handleTextSelection = useCallback(() => {
    if (isEditing) return

    const selection = window.getSelection()
    if (!selection || selection.isCollapsed) return

    const text = selection.toString().trim()
    if (text && taskBodyRef.current?.contains(selection.anchorNode)) {
      setSelectedText(text)
      // Focus the refinement input after selection
      setTimeout(() => {
        refinementInputRef.current?.focus()
      }, 0)
    }
  }, [isEditing])

  // Handle clicks outside task body and input area to clear selection
  const handleContainerClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as HTMLElement

    // Don't clear if clicking on the task body, refinement input, or selected text indicator
    if (
      taskBodyRef.current?.contains(target) ||
      refinementInputRef.current?.contains(target) ||
      target.closest('[data-selected-text-indicator]')
    ) {
      return
    }

    // Clear selection
    setSelectedText(null)
    window.getSelection()?.removeAllRanges()
  }, [])

  const handleSaveDescription = () => {
    if (editedDescription !== task?.current_description) {
      updateMutation.mutate({ current_description: editedDescription })
    } else {
      setIsEditing(false)
    }
  }

  const handleAgentSelect = (agentId: string) => {
    updateMutation.mutate({ selected_agent_id: agentId || undefined })
    setAgentSelectorOpen(false)
    // Focus the refinement input after modal closes
    setTimeout(() => {
      refinementInputRef.current?.focus()
    }, 100)
  }

  const handleRefinementSubmit = () => {
    if (!refinementComment.trim() || refineMutation.isPending) return
    refineMutation.mutate({
      userComment: refinementComment.trim(),
      userSelectedText: selectedText,
    })
  }

  const handleRefinementKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleRefinementSubmit()
    }
  }

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !task) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading task</p>
        <Button
          variant="outline"
          onClick={() => navigate({ to: "/tasks" })}
          className="mt-4"
        >
          Back to Tasks
        </Button>
      </div>
    )
  }

  const agents = agentsData?.data || []
  const sessions = sessionsData?.data || []
  const canExecute =
    task.selected_agent_id &&
    !["running", "pending_input", "completed", "archived"].includes(task.status)

  // Get the latest AI reply from refinement history
  const refinementHistory = (task.refinement_history || []) as RefinementHistoryItem[]
  const latestAiReply = [...refinementHistory].reverse().find(item => item.role === "ai")

  return (
    <div
      className={cn(
        "flex flex-col h-full transition-colors",
        isDraggingOver && "border-2 border-dashed border-primary bg-primary/5"
      )}
      onClick={handleContainerClick}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Drag overlay */}
      {isDraggingOver && (
        <div className="absolute inset-0 flex items-center justify-center bg-background/80 z-50 pointer-events-none">
          <div className="text-lg font-medium text-primary">Drop files to attach</div>
        </div>
      )}
      {/* Main content - centered task text */}
      <div className="flex-1 overflow-auto flex items-start justify-center p-6">
        <div className="w-full max-w-3xl">
          {isEditing ? (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">Editing task description</span>
                <div className="flex items-center gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setEditedDescription(task.current_description)
                      setIsEditing(false)
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    size="sm"
                    onClick={handleSaveDescription}
                    disabled={updateMutation.isPending}
                  >
                    {updateMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      "Save"
                    )}
                  </Button>
                </div>
              </div>
              <Textarea
                value={editedDescription}
                onChange={(e) => setEditedDescription(e.target.value)}
                className="min-h-[300px] resize-none text-base"
                placeholder="Task description..."
              />
            </div>
          ) : (
            <div
              ref={taskBodyRef}
              onMouseUp={handleTextSelection}
              className="prose prose-lg dark:prose-invert max-w-none selection:bg-amber-200 dark:selection:bg-amber-700"
            >
              <MarkdownRenderer
                content={task.current_description}
                className="text-base leading-relaxed"
              />
            </div>
          )}
        </div>
      </div>

      {/* Footer with 3 sections - full height elements */}
      <div className="border-t bg-background/60 p-4 shrink-0 relative">
        {/* Chat history overlay - game console style, 1/3 width aligned left */}
        {chatHistoryOpen && refinementHistory.length > 0 && (
          <div
            className="absolute bottom-full left-4 w-1/3 bg-slate-100/95 dark:bg-slate-900/90 backdrop-blur-sm border border-slate-300 dark:border-slate-700 rounded-t-lg max-h-[50vh] overflow-y-auto"
            onClick={() => setChatHistoryOpen(false)}
          >
            <div className="p-3 space-y-2">
              {refinementHistory.map((item, index) => (
                <div
                  key={index}
                  className={cn(
                    "flex gap-2",
                    item.role === "user" ? "flex-row-reverse" : ""
                  )}
                  onClick={(e) => e.stopPropagation()}
                >
                  <div
                    className={cn(
                      "max-w-[85%] rounded-lg px-2.5 py-1.5",
                      item.role === "user"
                        ? "bg-primary text-primary-foreground"
                        : "bg-slate-200 text-slate-800 dark:bg-slate-800 dark:text-slate-200"
                    )}
                  >
                    <p className="text-xs whitespace-pre-wrap">{item.content}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Selected text indicator */}
        {selectedText && (
          <div
            data-selected-text-indicator
            className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 max-w-2xl"
          >
            <div className="flex items-center gap-2 bg-amber-100 dark:bg-amber-900/70 text-amber-800 dark:text-amber-200 rounded-lg px-3 py-2 shadow-md border border-amber-300 dark:border-amber-700">
              <TextSelect className="h-4 w-4 shrink-0" />
              <span className="text-xs truncate max-w-md italic">
                "{selectedText.length > 100 ? selectedText.slice(0, 100) + "..." : selectedText}"
              </span>
              <button
                onClick={() => {
                  setSelectedText(null)
                  window.getSelection()?.removeAllRanges()
                }}
                className="shrink-0 p-0.5 rounded hover:bg-amber-200 dark:hover:bg-amber-800 transition-colors"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          </div>
        )}

        {/* Attached files display */}
        {attachedFiles.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-3 max-w-7xl mx-auto">
            {attachedFiles.map((file) => (
              <FileBadge
                key={file.id}
                file={file}
                onRemove={() => detachFileMutation.mutate(file.id)}
              />
            ))}
          </div>
        )}

        <div className="flex items-stretch gap-2 max-w-7xl mx-auto h-[80px]">
          {/* Left section - Latest AI reply (fills space to left, 30% transparent, compact) */}
          {latestAiReply && (
            <div
              className={cn(
                "flex-1 min-w-0 rounded-md px-2.5 py-2 cursor-pointer transition-all overflow-hidden relative",
                "bg-slate-500/30 dark:bg-slate-600/30",
                "border border-slate-400/30 dark:border-slate-500/30",
                "hover:bg-slate-500/40 dark:hover:bg-slate-600/40"
              )}
              onClick={() => setChatHistoryOpen(!chatHistoryOpen)}
            >
              <Bot className="h-3 w-3 text-slate-400/60 dark:text-slate-500/60 absolute top-1.5 left-1.5" />
              <p className="text-xs text-slate-600 dark:text-slate-300 line-clamp-4 leading-relaxed pl-4">
                {latestAiReply.content}
              </p>
            </div>
          )}

          {/* Center section - Message input (full height) */}
          <div className={cn("min-w-0", latestAiReply ? "flex-[2]" : "flex-1")}>
            <Textarea
              ref={refinementInputRef}
              value={refinementComment}
              onChange={(e) => setRefinementComment(e.target.value)}
              onKeyDown={handleRefinementKeyDown}
              placeholder="Add details, ask for changes, or request clarification..."
              className="h-full resize-none text-sm"
              disabled={refineMutation.isPending}
            />
          </div>

          {/* Attach file button */}
          <Button
            variant="outline"
            size="icon"
            onClick={() => setShowFileModal(true)}
            disabled={uploadFileMutation.isPending || attachFileMutation.isPending}
            className="h-full w-[48px] shrink-0"
            title="Attach files"
          >
            {uploadFileMutation.isPending || attachFileMutation.isPending ? (
              <Loader2 className="h-5 w-5 animate-spin" />
            ) : (
              <Plus className="h-5 w-5" />
            )}
          </Button>

          {/* Right section - Big square buttons */}
          <div className="flex items-stretch gap-2 shrink-0">
            {/* Send/Refine button - show Refine when input empty and no chat history */}
            {refinementHistory.length === 0 && !refinementComment.trim() ? (
              <Button
                variant="outline"
                onClick={() => autoRefineMutation.mutate()}
                disabled={autoRefineMutation.isPending}
                className="h-full w-[80px] flex-col gap-1 hover:text-amber-500 hover:border-amber-500/50 hover:bg-amber-500/10 transition-colors [&:hover_svg]:drop-shadow-[0_0_6px_rgba(251,191,36,0.6)]"
              >
                {autoRefineMutation.isPending ? (
                  <Loader2 className="h-5 w-5 animate-spin text-amber-500" />
                ) : (
                  <>
                    <Sparkles className="h-5 w-5 text-amber-500" />
                    <span className="text-xs">Refine</span>
                  </>
                )}
              </Button>
            ) : (
              <Button
                variant="outline"
                onClick={handleRefinementSubmit}
                disabled={!refinementComment.trim() || refineMutation.isPending}
                className="h-full w-[80px] flex-col gap-1"
              >
                {refineMutation.isPending ? (
                  <Loader2 className="h-5 w-5 animate-spin" />
                ) : (
                  <>
                    <Send className="h-5 w-5" />
                    <span className="text-xs">Send</span>
                  </>
                )}
              </Button>
            )}

            {/* Go to Session button - when sessions exist */}
            {sessions.length > 0 && (
              <Button
                variant="outline"
                onClick={() => {
                  // Navigate to the latest session (first in list since ordered by created_at desc)
                  navigate({
                    to: "/session/$sessionId",
                    params: { sessionId: sessions[0].id },
                    search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined },
                  })
                }}
                className="h-full w-[80px] flex-col gap-1"
              >
                <ExternalLink className="h-5 w-5" />
                <span className="text-xs">Session</span>
              </Button>
            )}

            {/* Sessions count button - when multiple sessions exist */}
            {sessions.length > 1 && (
              <Button
                variant="ghost"
                onClick={() => setSessionsModalOpen(true)}
                className="h-full w-[80px] flex-col gap-1 text-muted-foreground"
              >
                <Layers className="h-5 w-5" />
                <span className="text-xs">{sessions.length} sessions</span>
              </Button>
            )}

            {/* Send Answer button - for email-originated tasks */}
            {task.source_email_message_id && ["completed", "error"].includes(task.status) && (
              <Button
                variant="outline"
                onClick={() => sendAnswerMutation.mutate()}
                disabled={sendAnswerMutation.isPending}
                className="h-full w-[80px] flex-col gap-1"
              >
                {sendAnswerMutation.isPending ? (
                  <Loader2 className="h-5 w-5 animate-spin" />
                ) : (
                  <>
                    <Mail className="h-5 w-5" />
                    <span className="text-xs">Send Answer</span>
                  </>
                )}
              </Button>
            )}

            {/* Execute button - big square prominent */}
            <Button
              onClick={() => executeMutation.mutate()}
              disabled={!canExecute || executeMutation.isPending}
              className="h-full w-[80px] flex-col gap-1"
            >
              {executeMutation.isPending ? (
                <Loader2 className="h-5 w-5 animate-spin" />
              ) : (
                <>
                  <Play className="h-5 w-5" />
                  <span className="text-xs">{sessions.length > 0 ? "Run Again" : "Execute"}</span>
                </>
              )}
            </Button>
          </div>
        </div>
      </div>

      {/* Agent selection modal */}
      <Dialog open={agentSelectorOpen} onOpenChange={setAgentSelectorOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Select Agent</DialogTitle>
          </DialogHeader>
          <div className="flex flex-wrap gap-2 pt-4">
            {agents.map((agent) => {
              const colorPreset = getColorPreset(agent.ui_color_preset)
              const isSelected = task.selected_agent_id === agent.id
              return (
                <button
                  key={agent.id}
                  className={cn(
                    "cursor-pointer px-4 py-2 text-sm rounded-md transition-all flex items-center gap-2",
                    colorPreset.badgeBg,
                    colorPreset.badgeText,
                    colorPreset.badgeHover,
                    isSelected && colorPreset.badgeOutline
                  )}
                  onClick={() => handleAgentSelect(agent.id)}
                >
                  {agent.name}
                  {isSelected && <Check className="h-4 w-4" />}
                </button>
              )
            })}
            {agents.length === 0 && (
              <p className="text-sm text-muted-foreground">No agents available</p>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Task</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this task? This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                "Delete"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Sessions modal */}
      <TaskSessionsModal
        taskId={taskId}
        open={sessionsModalOpen}
        onOpenChange={setSessionsModalOpen}
      />

      {/* Trigger management modal */}
      <TriggerManagementModal
        taskId={taskId}
        open={triggerModalOpen}
        onOpenChange={setTriggerModalOpen}
      />

      {/* File upload modal */}
      <FileUploadModal
        open={showFileModal}
        onOpenChange={setShowFileModal}
        onFileUploaded={handleFileUploaded}
      />
    </div>
  )
}
