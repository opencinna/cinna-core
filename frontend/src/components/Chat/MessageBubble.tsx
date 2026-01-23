import { useState, useEffect, useRef } from "react"
import { formatDistanceToNow } from "date-fns"
import { Link } from "@tanstack/react-router"
import { toast } from "sonner"
import type { MessagePublic } from "@/client"
import { StreamEventRenderer } from "./StreamEventRenderer"
import { MessageActions } from "./MessageActions"
import { AnswerQuestionsModal } from "./AnswerQuestionsModal"
import { FileBadge } from "./FileBadge"
import { Info, AlertCircle, ExternalLink } from "lucide-react"
import { useToolApproval } from "@/hooks/useToolApproval"

interface MessageBubbleProps {
  message: MessagePublic
  onSendAnswer?: (content: string, answersToMessageId: string) => void
  onSendMessage?: (content: string) => void
  conversationModeUi?: string
  agentId?: string
}

export function MessageBubble({ message, onSendAnswer, onSendMessage, conversationModeUi = "detailed", agentId }: MessageBubbleProps) {
  const [showAnswerModal, setShowAnswerModal] = useState(false)
  const approvalMessageSentRef = useRef(false)

  // Tool approval hook
  const {
    toolsNeedingApproval,
    hasToolsNeedingApproval,
    isApproving,
    isApproved,
    error: approvalError,
    approveTools,
  } = useToolApproval({ message, agentId })

  // Show toast and send continue message on approval success
  useEffect(() => {
    if (isApproved && !approvalMessageSentRef.current) {
      approvalMessageSentRef.current = true
      toast.success("Tools approved", {
        description: `${toolsNeedingApproval.length} tool${toolsNeedingApproval.length > 1 ? "s" : ""} approved successfully`,
      })
      // Send message to trigger agent to continue
      if (onSendMessage) {
        onSendMessage("Tools approved — continue.")
      }
    }
  }, [isApproved, toolsNeedingApproval.length, onSendMessage])

  useEffect(() => {
    if (approvalError) {
      toast.error("Failed to approve tools", {
        description: approvalError,
      })
    }
  }, [approvalError])

  const isUser = message.role === "user"
  const isSystem = message.role === "system"
  const isSystemError = isSystem && message.status === "error"

  // Check if this is a task creation message (task-based handover or inbox task)
  const isTaskCreatedMessage = isSystem && message.message_metadata?.task_created === true
  const isInboxTask = message.message_metadata?.inbox_task === true
  const taskId = message.message_metadata?.task_id as string | undefined
  const taskSessionId = message.message_metadata?.session_id as string | undefined

  if (isSystem) {
    return (
      <div className="flex justify-center my-4">
        <div
          className={`text-sm px-4 py-2 rounded-lg max-w-2xl ${
            isSystemError
              ? "bg-destructive/10 text-destructive border border-destructive/20"
              : isTaskCreatedMessage
              ? "bg-blue-50/60 dark:bg-blue-950/20 text-blue-900 dark:text-blue-100 border border-blue-200 dark:border-blue-800"
              : "bg-muted/60 text-muted-foreground"
          }`}
        >
          <div className="flex items-center gap-2">
            <span>{message.content}</span>
            {isTaskCreatedMessage && isInboxTask && taskId && (
              <Link
                to="/task/$taskId"
                params={{ taskId: taskId }}
                className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 underline underline-offset-2"
              >
                <span>View task</span>
                <ExternalLink className="h-3 w-3" />
              </Link>
            )}
            {isTaskCreatedMessage && !isInboxTask && taskSessionId && (
              <Link
                to="/session/$sessionId"
                params={{ sessionId: taskSessionId }}
                search={{ initialMessage: undefined, fileIds: undefined, fileObjects: undefined }}
                className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 underline underline-offset-2"
              >
                <span>View session</span>
                <ExternalLink className="h-3 w-3" />
              </Link>
            )}
          </div>
        </div>
      </div>
    )
  }

  // Extract metadata for display
  const model = message.message_metadata?.model as string | undefined
  const totalCost = message.message_metadata?.total_cost_usd as number | undefined
  const durationMs = message.message_metadata?.duration_ms as number | undefined
  const numTurns = message.message_metadata?.num_turns as number | undefined
  const streamingEvents = (message.message_metadata?.streaming_events || []) as any[]

  // Extract status
  const messageStatus = message.status || ""
  const isInterrupted = messageStatus === "user_interrupted"
  const isError = messageStatus === "error"

  // Extract and deduplicate questions from AskUserQuestion tool calls
  const extractQuestions = () => {
    const questions: any[] = []
    const seen = new Set<string>()

    streamingEvents.forEach((event) => {
      if (event.type === "tool" && event.tool_name === "AskUserQuestion") {
        const toolQuestions = event.metadata?.tool_input?.questions || []
        toolQuestions.forEach((q: any) => {
          if (!seen.has(q.question)) {
            seen.add(q.question)
            questions.push(q)
          }
        })
      }
    })

    return questions
  }

  const questions = extractQuestions()
  const hasQuestions = message.tool_questions_status === "unanswered" && questions.length > 0

  // Handle answer submission
  const handleAnswerSubmit = (answers: string, messageId: string) => {
    if (onSendAnswer) {
      onSendAnswer(answers, messageId)
    }
  }

  // Format UTC timestamp - handle invalid dates
  let formattedTime = "Just now"
  let utcTimestamp = ""

  try {
    if (message.timestamp) {
      // Handle timestamp - it might already have 'Z' from optimistic updates
      const timestampStr = typeof message.timestamp === 'string'
        ? (message.timestamp.endsWith('Z') ? message.timestamp : message.timestamp + 'Z')
        : message.timestamp

      const date = new Date(timestampStr)
      if (!isNaN(date.getTime())) {
        formattedTime = formatDistanceToNow(date, { addSuffix: true })
        utcTimestamp = date.toUTCString()
      }
    }
  } catch (error) {
    console.error("Error formatting timestamp:", error, message.timestamp)
  }

  const durationSec = durationMs ? (durationMs / 1000).toFixed(2) : null

  return (
    <>
      <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-2`}>
        <div className="max-w-[70%]">
          <div
            className={`rounded-lg px-4 py-3 ${
              isUser
                ? "bg-green-50/60 dark:bg-green-950/20 border border-green-100/50 dark:border-green-900/30 text-foreground shadow-sm"
                : "bg-muted/60 text-foreground"
            }`}
          >
            <div className="space-y-2">
              {isUser ? (
                <p className="whitespace-pre-wrap break-words">{message.content}</p>
              ) : (
                <StreamEventRenderer events={streamingEvents} conversationModeUi={conversationModeUi} />
              )}

              {/* File badges */}
              {message.files && message.files.length > 0 && (
                <div className="flex flex-wrap gap-1 pt-2 border-t border-border/50">
                  {message.files.map((file: any) => (
                    <FileBadge
                      key={file.id}
                      file={file}
                      downloadable={true}
                    />
                  ))}
                </div>
              )}

              <div className="flex items-center justify-between gap-2">
                <p
                  className={`text-xs ${
                    isUser ? "text-muted-foreground" : "text-muted-foreground"
                  }`}
                >
                  {formattedTime}
                </p>

                <div className="flex items-center gap-1.5">
                  {/* Status badge for interrupted/error messages */}
                  {!isUser && (isInterrupted || isError) && (
                    <div
                      className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs ${
                        isInterrupted
                          ? "bg-yellow-100 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-200"
                          : "bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200"
                      }`}
                      title={message.status_message || (isInterrupted ? "Interrupted by user" : "Error occurred")}
                    >
                      <AlertCircle className="h-3 w-3" />
                      <span>{isInterrupted ? "Interrupted" : "Error"}</span>
                    </div>
                  )}

                  {/* Existing info icon */}
                  {!isUser && (model || totalCost || durationSec || numTurns) && (
                    <div
                      className="cursor-help"
                      title={[
                        `UTC Time: ${utcTimestamp}`,
                        model ? `Model: ${model}` : null,
                        totalCost ? `Cost: $${totalCost.toFixed(6)} USD` : null,
                        durationSec ? `Duration: ${durationSec}s` : null,
                        numTurns ? `Turns: ${numTurns}` : null,
                      ]
                        .filter(Boolean)
                        .join('\n')}
                    >
                      <Info className="w-3.5 h-3.5 text-muted-foreground/40 hover:text-muted-foreground/70 transition-colors" />
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Message Actions - Outside bubble, aligned right, within same width container */}
          {!isUser && (hasQuestions || hasToolsNeedingApproval) && (
            <MessageActions
              message={message}
              onAnswerQuestions={hasQuestions ? () => setShowAnswerModal(true) : undefined}
              hasToolsNeedingApproval={hasToolsNeedingApproval}
              toolsNeedingApproval={toolsNeedingApproval}
              isApprovingTools={isApproving}
              onApproveTools={approveTools}
            />
          )}
        </div>
      </div>

      {/* Answer Questions Modal */}
      {!isUser && hasQuestions && (
        <AnswerQuestionsModal
          open={showAnswerModal}
          onOpenChange={setShowAnswerModal}
          questions={questions}
          messageId={message.id}
          onSubmit={handleAnswerSubmit}
        />
      )}
    </>
  )
}
