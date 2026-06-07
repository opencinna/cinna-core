import { useState, useEffect, useRef } from "react"
import { formatDistanceToNow } from "date-fns"
import { Link } from "@tanstack/react-router"
import { toast } from "sonner"
import type { MessagePublic, FileUploadPublic } from "@/client"
import { StreamEventRenderer } from "./StreamEventRenderer"
import { MarkdownRenderer } from "./MarkdownRenderer"
import { MessageActions } from "./MessageActions"
import { AnswerQuestionsModal } from "./AnswerQuestionsModal"
import { FileBadge } from "./FileBadge"
import { AttachmentPreviewModal } from "./AttachmentPreviewModal"
import { Button } from "@/components/ui/button"
import { Info, AlertCircle, ExternalLink, CheckCircle2, HelpCircle, AlertTriangle, Mail, RefreshCw, Clock, Terminal, Copy, Check, XCircle } from "lucide-react"
import { useToolApproval } from "@/hooks/useToolApproval"
import { RecoverSessionModal } from "./RecoverSessionModal"

interface MessageBubbleProps {
  message: MessagePublic
  onSendAnswer?: (content: string, answersToMessageId: string) => void
  onSendMessage?: (content: string) => void
  conversationModeUi?: string
  agentId?: string
  integrationTyp?: string | null
  sessionId?: string
}

export function MessageBubble({ message, onSendAnswer, onSendMessage, conversationModeUi = "detailed", agentId, integrationTyp, sessionId }: MessageBubbleProps) {
  const [showAnswerModal, setShowAnswerModal] = useState(false)
  const [showRecoverModal, setShowRecoverModal] = useState(false)
  const [commandOutputCopied, setCommandOutputCopied] = useState(false)
  const [previewFile, setPreviewFile] = useState<FileUploadPublic | null>(null)
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

  // Pre-LLM install-readiness gate synthesised a "setup needed" reply —
  // render it with a distinctive warning style and a link to the agent's
  // Credentials tab where the user can fill in the missing values.
  const isInstallSetupRequired =
    isSystem && message.message_metadata?.install_setup_required === true
  const installSetupGateStatus = message.message_metadata?.gate_status as
    | string
    | undefined

  // Check if this is a task feedback message (from sub-task agent)
  const isTaskFeedback = isUser && message.message_metadata?.task_feedback === true
  const feedbackState = message.message_metadata?.task_state as string | undefined
  const feedbackTaskId = message.message_metadata?.task_id as string | undefined

  if (isTaskFeedback) {
    const stateConfig = {
      completed: { icon: CheckCircle2, color: "border-green-500", iconColor: "text-green-600 dark:text-green-400", bg: "bg-green-50/60 dark:bg-green-950/20" },
      needs_input: { icon: HelpCircle, color: "border-amber-500", iconColor: "text-amber-600 dark:text-amber-400", bg: "bg-amber-50/60 dark:bg-amber-950/20" },
      error: { icon: AlertTriangle, color: "border-red-500", iconColor: "text-red-600 dark:text-red-400", bg: "bg-red-50/60 dark:bg-red-950/20" },
    }[feedbackState || "completed"] || { icon: CheckCircle2, color: "border-blue-500", iconColor: "text-blue-600", bg: "bg-blue-50/60 dark:bg-blue-950/20" }

    const Icon = stateConfig.icon

    return (
      <div className="flex justify-center my-4">
        <div className={`text-sm px-4 py-3 rounded-lg max-w-2xl border-l-4 ${stateConfig.color} ${stateConfig.bg}`}>
          <div className="flex items-start gap-2">
            <Icon className={`h-4 w-4 mt-0.5 shrink-0 ${stateConfig.iconColor}`} />
            <div className="flex-1 min-w-0">
              <span className="text-foreground">{message.content}</span>
              {feedbackTaskId && (
                <Link
                  to="/session/$sessionId"
                  params={{ sessionId: feedbackTaskId }}
                  search={{ initialMessage: undefined, fileIds: undefined, fileObjects: undefined, pageContext: undefined }}
                  className="ml-2 inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 underline underline-offset-2 text-xs"
                >
                  <span>View session</span>
                  <ExternalLink className="h-3 w-3" />
                </Link>
              )}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // Command response messages (role=system, metadata.command=true) are rendered
  // by the command-specific branches further down (isCommandStream / isCommand)
  // via MarkdownRenderer or the terminal-style block — don't short-circuit here.
  const isCommandSystemMessage = isSystem && message.message_metadata?.command === true

  if (isInstallSetupRequired) {
    const isPublisherBroken = installSetupGateStatus === "publisher_broken"
    return (
      <div className="flex justify-center my-4">
        <div className="max-w-2xl w-full">
          <div
            className={`text-sm px-4 py-3 rounded-lg border-l-4 ${
              isPublisherBroken
                ? "border-red-500 bg-red-50/70 dark:bg-red-950/30 text-red-900 dark:text-red-100"
                : "border-amber-500 bg-amber-50/70 dark:bg-amber-950/30 text-amber-900 dark:text-amber-100"
            }`}
          >
            <div className="flex items-start gap-2">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0 space-y-2">
                <p className="font-medium">
                  {isPublisherBroken
                    ? "Publisher credentials unavailable"
                    : "Setup needed before this agent can run"}
                </p>
                <p className="whitespace-pre-wrap break-words">
                  {message.content}
                </p>
                {!isPublisherBroken && agentId && (
                  <p className="text-xs">
                    Open the{" "}
                    <Link
                      to="/agent/$agentId"
                      params={{ agentId }}
                      hash="credentials"
                      className="underline underline-offset-2 hover:no-underline"
                    >
                      Credentials tab
                    </Link>{" "}
                    on this agent and fill in the missing credentials one by
                    one.
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (isSystem && !isCommandSystemMessage) {
    return (
      <>
        <div className="flex justify-center my-4">
          <div className="max-w-2xl">
            <div
              className={`text-sm px-4 py-2 rounded-lg ${
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
                    search={{ initialMessage: undefined, fileIds: undefined, fileObjects: undefined, pageContext: undefined }}
                    className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 underline underline-offset-2"
                  >
                    <span>View session</span>
                    <ExternalLink className="h-3 w-3" />
                  </Link>
                )}
              </div>
            </div>
            {isSystemError && sessionId && (
              <div className="mt-2 flex gap-2 justify-end flex-wrap">
                <Button
                  variant="outline"
                  size="sm"
                  className="text-amber-700 dark:text-amber-300 border-amber-300 dark:border-amber-700 hover:bg-amber-50 dark:hover:bg-amber-950"
                  onClick={() => setShowRecoverModal(true)}
                >
                  <RefreshCw className="h-4 w-4 mr-1.5" />
                  Recover Session
                </Button>
              </div>
            )}
          </div>
        </div>
        {isSystemError && sessionId && (
          <RecoverSessionModal
            open={showRecoverModal}
            onOpenChange={setShowRecoverModal}
            sessionId={sessionId}
          />
        )}
      </>
    )
  }

  // Check if this is a pending user message (sent but not yet delivered to agent)
  const isPendingMessage = isUser && message.sent_to_agent_status === "pending"

  // Check if this is a command response (e.g. /files) - rendered directly, not via streaming events
  const isCommand = message.message_metadata?.command === true
  // Check if this is a streaming command output message (/run:name)
  const isCommandStream = isCommand && message.message_metadata?.routing === "command_stream"
  const commandName = message.message_metadata?.command_name as string | undefined
  const resolvedCommand = message.message_metadata?.resolved_command as string | undefined
  const streamingInProgress = message.message_metadata?.streaming_in_progress === true
  const execExitCode = message.message_metadata?.exec_exit_code as number | null | undefined
  const execTimedOut = message.message_metadata?.exec_timed_out === true
  const execInterrupted = message.message_metadata?.exec_interrupted === true
  const execTruncated = message.message_metadata?.exec_truncated === true

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
      if (event.type === "tool" && event.tool_name?.toLowerCase() === "askuserquestion") {
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

  // Handle copy for command output
  const handleCopyCommandOutput = () => {
    const outputChunks = streamingEvents
      .filter((e: any) => e.type === "tool_result_delta" && e.content)
      .map((e: any) => e.content as string)
    const text = outputChunks.join("")
    navigator.clipboard.writeText(text).then(() => {
      setCommandOutputCopied(true)
      setTimeout(() => setCommandOutputCopied(false), 2000)
    })
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
                <>
                  {integrationTyp === "email" && (
                    <div className="flex items-center gap-1 text-[10px] font-medium text-indigo-600 dark:text-indigo-400 mb-1">
                      <Mail className="h-3 w-3" />
                      <span>via Email</span>
                    </div>
                  )}
                  <p className="whitespace-pre-wrap break-words">{message.content}</p>
                </>
              ) : isCommandStream ? (
                // Command stream output (/run:<name>)
                <div className="space-y-2">
                  {/* Header */}
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 text-xs text-muted-foreground min-w-0">
                      <Terminal className="h-3.5 w-3.5 shrink-0" />
                      <span className="font-mono font-medium truncate">{commandName || "/run"}</span>
                      {resolvedCommand && (
                        <span
                          className="text-muted-foreground/60 truncate"
                          title={resolvedCommand}
                        >
                          → {resolvedCommand.length > 80 ? resolvedCommand.slice(0, 80) + "…" : resolvedCommand}
                        </span>
                      )}
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 px-2 text-xs shrink-0"
                      onClick={handleCopyCommandOutput}
                      title="Copy output"
                    >
                      {commandOutputCopied ? (
                        <><Check className="h-3 w-3 mr-1" />Copied</>
                      ) : (
                        <><Copy className="h-3 w-3 mr-1" />Copy</>
                      )}
                    </Button>
                  </div>

                  {/* Output area */}
                  {(() => {
                    const outputEvents = streamingEvents
                      .filter((e: any) => e.type === "tool_result_delta" && e.content)
                      .sort((a: any, b: any) => (a.event_seq || 0) - (b.event_seq || 0))

                    const hasOutput = outputEvents.length > 0

                    // Streaming: terminal-style live view
                    if (streamingInProgress) {
                      return (
                        <div
                          className="bg-slate-950 dark:bg-slate-950 rounded text-xs font-mono overflow-x-auto max-h-[400px] overflow-y-auto"
                          role="log"
                          aria-live="polite"
                        >
                          {hasOutput ? (
                            <pre className="p-3 whitespace-pre-wrap break-all text-slate-100">
                              {outputEvents.map((e: any, idx: number) => (
                                <span
                                  key={e.event_seq ?? idx}
                                  className={e.metadata?.stream === "stderr" ? "text-amber-400" : ""}
                                >
                                  {e.content}
                                </span>
                              ))}
                              <span className="animate-pulse">▋</span>
                            </pre>
                          ) : (
                            <div className="p-3 text-slate-400">
                              Running<span className="animate-pulse">▋</span>
                            </div>
                          )}
                        </div>
                      )
                    }

                    // Completed: markdown-rendered output so scripts that
                    // emit markdown (tables, lists, headings) render properly.
                    if (!hasOutput) {
                      return (
                        <div className="bg-muted/40 rounded p-3 text-xs text-muted-foreground italic">
                          Command completed — no output.
                        </div>
                      )
                    }

                    const joinedOutput = outputEvents.map((e: any) => e.content).join("")

                    return (
                      <div className="bg-muted/40 rounded p-3 overflow-x-auto max-h-[400px] overflow-y-auto">
                        <MarkdownRenderer
                          content={joinedOutput}
                          className="prose dark:prose-invert max-w-none text-sm prose-p:leading-normal prose-p:my-2 prose-ul:my-2 prose-li:my-0 prose-pre:my-2"
                        />
                      </div>
                    )
                  })()}

                  {/* Footer */}
                  {!streamingInProgress && (
                    <div className="flex items-center gap-2 text-xs">
                      {execTimedOut ? (
                        <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                          <AlertTriangle className="h-3 w-3" />Timed out
                        </span>
                      ) : execInterrupted ? (
                        <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                          <AlertCircle className="h-3 w-3" />Interrupted
                        </span>
                      ) : execExitCode === 0 ? (
                        <span className="flex items-center gap-1 text-green-600 dark:text-green-400">
                          <CheckCircle2 className="h-3 w-3" />Exit 0
                        </span>
                      ) : execExitCode != null ? (
                        <span className="flex items-center gap-1 text-red-600 dark:text-red-400">
                          <XCircle className="h-3 w-3" />Exit {execExitCode}
                        </span>
                      ) : null}
                      {execTruncated && (
                        <span className="text-amber-600 dark:text-amber-400">[truncated]</span>
                      )}
                    </div>
                  )}
                </div>
              ) : isCommand ? (
                <MarkdownRenderer
                  content={message.content || ""}
                  className="prose dark:prose-invert max-w-none prose-p:leading-normal prose-p:my-2 prose-ul:my-2 prose-li:my-0"
                />
              ) : (
                <StreamEventRenderer events={streamingEvents} conversationModeUi={conversationModeUi} />
              )}

              {/* File badges.
                  User uploads always render as downloadable badges. Agent
                  attachments normally render inline via `attachment` events in
                  the streaming trace (StreamEventRenderer → AttachmentBlock);
                  we only fall back to rendering them here as preview badges
                  when this message carries no attachment events. */}
              {(() => {
                const files = (message.files || []) as FileUploadPublic[]
                if (files.length === 0) return null

                const userFiles = files.filter(
                  (f) => f.source !== "agent_attachment"
                )
                const agentFiles = files.filter(
                  (f) => f.source === "agent_attachment"
                )

                const hasAttachmentEvents = streamingEvents.some(
                  (e: any) => e.type === "attachment"
                )
                // Render agent attachments as a fallback only when there is no
                // inline event trace for them.
                const fallbackAgentFiles = hasAttachmentEvents ? [] : agentFiles

                const badgeFiles = [...userFiles, ...fallbackAgentFiles]
                if (badgeFiles.length === 0) return null

                return (
                  <div className="flex flex-wrap gap-1 pt-2 border-t border-border/50">
                    {badgeFiles.map((file) => {
                      const isAgentAttachment =
                        file.source === "agent_attachment"
                      return (
                        <FileBadge
                          key={file.id}
                          file={file}
                          downloadable={!isAgentAttachment}
                          onPreview={
                            isAgentAttachment
                              ? (f) => setPreviewFile(f)
                              : undefined
                          }
                        />
                      )
                    })}
                  </div>
                )
              })()}

              <div className="flex items-center justify-between gap-2">
                <p
                  className={`text-xs ${
                    isUser ? "text-muted-foreground" : "text-muted-foreground"
                  }`}
                >
                  {formattedTime}
                </p>

                <div className="flex items-center gap-1.5">
                  {/* Pending indicator — shown while the message is queued for the agent */}
                  {isPendingMessage && (
                    <div
                      className="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-amber-50 dark:bg-amber-950/30 text-amber-600 dark:text-amber-400"
                      title="Waiting for agent to pick up this message"
                    >
                      <Clock className="h-3 w-3 animate-pulse" />
                      <span>Pending</span>
                    </div>
                  )}
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

      {/* Attachment preview modal (fallback agent-attachment badges) */}
      {previewFile && (
        <AttachmentPreviewModal
          open={!!previewFile}
          onOpenChange={(open) => {
            if (!open) setPreviewFile(null)
          }}
          fileId={previewFile.id}
          filename={previewFile.filename}
          mimeType={previewFile.mime_type}
          size={previewFile.file_size}
        />
      )}

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
