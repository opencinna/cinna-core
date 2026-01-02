import { useState } from "react"
import { formatDistanceToNow } from "date-fns"
import { Link } from "@tanstack/react-router"
import type { MessagePublic } from "@/client"
import { StreamEventRenderer } from "./StreamEventRenderer"
import { MessageActions } from "./MessageActions"
import { AnswerQuestionsModal } from "./AnswerQuestionsModal"
import { Info, AlertCircle, ExternalLink } from "lucide-react"

interface MessageBubbleProps {
  message: MessagePublic
  onSendAnswer?: (content: string, answersToMessageId: string) => void
}

export function MessageBubble({ message, onSendAnswer }: MessageBubbleProps) {
  const [showAnswerModal, setShowAnswerModal] = useState(false)

  const isUser = message.role === "user"
  const isSystem = message.role === "system"
  const isSystemError = isSystem && message.status === "error"

  // Check if this is a handover system message
  const isHandoverMessage = isSystem && message.message_metadata?.handover_type === "agent_handover"
  const forwardedToSessionId = message.message_metadata?.forwarded_to_session_id as string | undefined
  const targetAgentName = message.message_metadata?.target_agent_name as string | undefined

  if (isSystem) {
    return (
      <div className="flex justify-center my-4">
        <div
          className={`text-sm px-4 py-2 rounded-lg max-w-2xl ${
            isSystemError
              ? "bg-destructive/10 text-destructive border border-destructive/20"
              : isHandoverMessage
              ? "bg-blue-50 dark:bg-blue-950/30 text-blue-900 dark:text-blue-100 border border-blue-200 dark:border-blue-800"
              : "bg-muted text-muted-foreground"
          }`}
        >
          <div className="flex items-center gap-2">
            <span>{message.content}</span>
            {isHandoverMessage && forwardedToSessionId && (
              <Link
                to="/session/$sessionId"
                params={{ sessionId: forwardedToSessionId }}
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
                ? "bg-gradient-to-br from-sky-50 to-blue-50 dark:from-sky-950/25 dark:to-blue-950/25 border border-sky-100/50 dark:border-sky-900/15 text-foreground shadow-sm"
                : "bg-muted text-foreground"
            }`}
          >
            <div className="space-y-2">
              {isUser ? (
                <p className="whitespace-pre-wrap break-words">{message.content}</p>
              ) : (
                <StreamEventRenderer events={streamingEvents} />
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
          {!isUser && hasQuestions && (
            <MessageActions
              message={message}
              onAnswerQuestions={() => setShowAnswerModal(true)}
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
