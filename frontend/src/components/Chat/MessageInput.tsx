import { useState, KeyboardEvent, forwardRef, DragEvent } from "react"
import { useMutation } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
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
import { Send, Square, Loader2, Plus, Paperclip, Sparkles } from "lucide-react"
import { RotatingHints } from "@/components/Common/RotatingHints"
import { FileUploadModal } from "./FileUploadModal"
import { GettingStartedModal } from "@/components/Onboarding/GettingStartedModal"
import { FileBadge } from "./FileBadge"
import { FilesService, UtilsService } from "@/client"
import type { FileUploadPublic } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"

interface MessageInputProps {
  onSend: (content: string, fileIds?: string[]) => void
  onStop?: () => void
  /** @deprecated Use isStreaming instead. Kept for backward compatibility. */
  sendDisabled?: boolean
  /** When true, the stop button is shown and the agent is responding. Input remains enabled so the user can queue messages. */
  isStreaming?: boolean
  isInterruptPending?: boolean
  placeholder?: string
  agentId?: string
  mode?: "building" | "conversation"
  isNewAgent?: boolean
}

export const MessageInput = forwardRef<HTMLTextAreaElement, MessageInputProps>(
  function MessageInput({
    onSend,
    onStop,
    sendDisabled = false,
    isStreaming = false,
    isInterruptPending = false,
    placeholder = "Type your message...",
    agentId,
    mode = "conversation",
    isNewAgent = false,
  }, ref) {
    // Resolve effective streaming state: prefer the new isStreaming prop, fall
    // back to the legacy sendDisabled prop so callers that haven't migrated yet
    // still work correctly.
    const effectiveIsStreaming = isStreaming || sendDisabled
    const [message, setMessage] = useState("")
    const [attachedFiles, setAttachedFiles] = useState<FileUploadPublic[]>([])
    const [showFileModal, setShowFileModal] = useState(false)
    const [isDraggingOver, setIsDraggingOver] = useState(false)
    const [showGettingStarted, setShowGettingStarted] = useState(false)
    const [isHovering, setIsHovering] = useState(false)
    const { showErrorToast } = useCustomToast()

    const refineMutation = useMutation({
      mutationFn: () =>
        UtilsService.refinePrompt({
          requestBody: {
            user_input: message,
            has_files_attached: attachedFiles.length > 0,
            agent_id: agentId || null,
            mode: mode,
            is_new_agent: isNewAgent,
          },
        }),
      onSuccess: (data) => {
        if (data.success && data.refined_prompt) {
          setMessage(data.refined_prompt)
        } else if (data.error) {
          showErrorToast(data.error)
        }
      },
      onError: (error: Error) => {
        showErrorToast(error.message || "Failed to refine prompt")
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

    const handleSend = () => {
      const trimmedMessage = message.trim()
      if (trimmedMessage || attachedFiles.length > 0) {
        const fileIds = attachedFiles.map(f => f.id)
        onSend(trimmedMessage, fileIds)
        setMessage("")
        setAttachedFiles([])
      }
    }

    const handleStop = () => {
      if (onStop) {
        onStop()
      }
    }

    const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault()
        handleSend()
      }
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
          console.error(`File ${file.name} is too large (max 100MB)`)
          return
        }
        uploadMutation.mutate(file)
      })
    }

    return (
      <div className="border-t p-4 bg-background/60 shrink-0">
        <div className="flex gap-2 items-end max-w-7xl mx-auto">
          {/* Add Button */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="icon"
                className="h-[60px] w-[60px] shrink-0"
              >
                <Plus className="h-5 w-5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem onClick={() => setShowFileModal(true)}>
                <Paperclip className="h-4 w-4 mr-2" />
                Attach File
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>

          {/* Textarea with drag-drop support */}
          <div
            className="relative flex-1 max-h-[60px] group"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onMouseEnter={() => setIsHovering(true)}
            onMouseLeave={() => setIsHovering(false)}
          >
            <Textarea
              ref={ref}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
              className={`min-h-[60px] max-h-[200px] resize-none transition-colors pr-12 ${
                isDraggingOver ? 'border-primary border-2 bg-primary/5' : ''
              }`}
              rows={2}
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
                      absolute bottom-2 right-2
                      p-1.5 rounded-md
                      transition-all duration-200
                      ${isHovering || refineMutation.isPending ? 'opacity-100' : 'opacity-0'}
                      ${refineMutation.isPending
                        ? 'text-amber-500 cursor-wait'
                        : 'text-muted-foreground hover:text-amber-500 hover:bg-amber-500/10 cursor-pointer'}
                    `}
                  >
                    <Sparkles
                      className={`h-4 w-4 ${refineMutation.isPending ? 'animate-pulse' : ''}`}
                      style={refineMutation.isPending ? {} : undefined}
                    />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="top">
                  <p>Refine prompt with AI</p>
                </TooltipContent>
              </Tooltip>
            )}
          </div>

          {/* Send / Stop Buttons */}
          <div className="flex flex-col gap-1 shrink-0">
            {effectiveIsStreaming && (
              <Button
                onClick={handleStop}
                variant="destructive"
                size="icon"
                className="h-[28px] w-[60px]"
                disabled={isInterruptPending}
                title="Stop agent"
              >
                {isInterruptPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Square className="h-4 w-4" />
                )}
              </Button>
            )}
            <Button
              onClick={handleSend}
              disabled={!message.trim() && attachedFiles.length === 0}
              size="icon"
              className={effectiveIsStreaming ? "h-[28px] w-[60px]" : "h-[60px] w-[60px]"}
              title="Send message"
            >
              <Send className="h-5 w-5" />
            </Button>
          </div>
        </div>

        {/* File Upload Modal */}
        <FileUploadModal
          open={showFileModal}
          onOpenChange={setShowFileModal}
          onFileUploaded={handleFileUploaded}
        />

        {/* Footer: Show attached files or rotating hints */}
        {attachedFiles.length > 0 ? (
          <div className="mt-2 max-w-7xl mx-auto">
            <div className="flex flex-wrap gap-2">
              {attachedFiles.map(file => (
                <FileBadge
                  key={file.id}
                  file={file}
                  onRemove={() => handleFileRemove(file.id)}
                />
              ))}
            </div>
          </div>
        ) : (
          <RotatingHints
            className="mt-2 max-w-7xl mx-auto"
            onClick={() => setShowGettingStarted(true)}
          />
        )}

        {/* Getting Started Modal */}
        <GettingStartedModal
          open={showGettingStarted}
          onOpenChange={setShowGettingStarted}
        />
      </div>
    )
  }
)
