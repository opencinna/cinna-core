import { useQuery, useQueryClient } from "@tanstack/react-query"
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react"

import { MessagesService, SessionsService } from "@/client"
import { ComponentRefChatContext } from "@/components/Chat/componentRefContext"
import { MessageInput } from "@/components/Chat/MessageInput"
import { MessageList } from "@/components/Chat/MessageList"
import type { ComponentPrompt } from "@/components/Dashboard/AgentComponents/componentSpec"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { useEventSubscription } from "@/hooks/useEventBus"
import { useSessionStreaming } from "@/hooks/useSessionStreaming"
import { type EventData, EventTypes } from "@/services/eventService"
import { componentsAlreadyExplained } from "./dashboardContext"
import { PromptStarters } from "./PromptStarters"

export type Seed = { text: string; nonce: number }

let seedNonce = 0
export function nextSeed(text: string): Seed {
  seedNonce += 1
  return { text, nonce: seedNonce }
}

export interface PendingSend {
  content: string
  context: string | undefined
  fileIds?: string[]
}

interface Props {
  sessionId: string | undefined
  /** Hidden conversations poll only while a turn runs (its end refreshes tiles). */
  visible: boolean
  agent: { id: string; name: string; conversation_mode_ui?: string | null }
  /** A seed from outside (a page's `ui/message`, Ask to fix); never sends. */
  seed: Seed | undefined
  onSeed: (text: string) => void
  /** The first message, sent once the session the parent just created mounts here. */
  pending: PendingSend | null
  onPendingTaken: () => void
  /** No session yet: the parent creates one, then hands the message back as `pending`. */
  onSendWithoutSession: (send: PendingSend) => void
  /** The `<dashboard_context>` for the next message, or nothing. */
  buildContext: (explained: Set<string>) => string | undefined
  onComposerFocus: () => void
  onMessageSent: () => void
  /** `stream_completed` for this session: the agent's tiles refresh or re-read. */
  onTurnEnd: (sessionId: string) => void
  starters: ComponentPrompt[]
  chip: ReactNode
  header: (state: { hasMessages: boolean }) => ReactNode
}

function MessagesSkeleton() {
  return (
    <div className="flex-1 space-y-3 p-3">
      <Skeleton className="ml-auto h-10 w-2/3 rounded-lg" />
      <Skeleton className="h-16 w-5/6 rounded-lg" />
      <Skeleton className="ml-auto h-10 w-1/2 rounded-lg" />
    </div>
  )
}

/**
 * One (dashboard, agent) session in the panel (UI spec S5): messages and
 * stream, starters, the context chip and the composer. Mounted per agent and
 * kept while hidden, so a draft and a running turn survive a switch.
 */
export function DashboardChatConversation({
  sessionId,
  visible,
  agent,
  seed,
  onSeed,
  pending,
  onPendingTaken,
  onSendWithoutSession,
  buildContext,
  onComposerFocus,
  onMessageSent,
  onTurnEnd,
  starters,
  chip,
  header,
}: Props) {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const [composerEmpty, setComposerEmpty] = useState(true)
  const [restoreSeed, setRestoreSeed] = useState<Seed | undefined>()
  const effectiveSeed =
    restoreSeed && (!seed || restoreSeed.nonce > seed.nonce)
      ? restoreSeed
      : seed

  const sessionQuery = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => SessionsService.getSession({ id: sessionId ?? "" }),
    enabled: !!sessionId,
    refetchInterval: (query) => {
      const status = query.state.data?.interaction_status
      if (status === "running" || status === "pending_stream") return 3000
      return visible ? 10000 : false
    },
  })
  const session = sessionQuery.data
  const streamingNow =
    session?.interaction_status === "running" ||
    session?.interaction_status === "pending_stream"

  const messagesQuery = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () =>
      MessagesService.getMessages({
        sessionId: sessionId ?? "",
        offset: 0,
        limit: 100,
      }),
    enabled: !!sessionId,
    refetchInterval: streamingNow ? 2000 : undefined,
  })
  const messages = messagesQuery.data?.data ?? []

  const {
    sendMessage,
    stopMessage,
    isStreaming,
    streamingEvents,
    isInterruptPending,
    isPending,
  } = useSessionStreaming({
    sessionId: sessionId ?? "",
    session: session
      ? { interaction_status: session.interaction_status, mode: session.mode }
      : null,
    messagesData: messagesQuery.data
      ? {
          data: messagesQuery.data.data as Array<{
            message_metadata?: Record<string, unknown>
          }>,
        }
      : null,
    onError: (error) =>
      showErrorToast(error.message || "Failed to send message"),
  })

  // The status event refreshes the session at once instead of on the next poll.
  const onStatusChanged = useCallback(
    (event: EventData) => {
      if (!sessionId || event.meta?.session_id !== sessionId) return
      queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
      if (event.meta?.interaction_status === "")
        queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
    },
    [queryClient, sessionId],
  )
  useEventSubscription(
    EventTypes.SESSION_INTERACTION_STATUS_CHANGED,
    onStatusChanged,
  )

  const turnEndRef = useRef(onTurnEnd)
  turnEndRef.current = onTurnEnd
  const onStreamCompleted = useCallback(
    (event: EventData) => {
      if (sessionId && event.meta?.session_id === sessionId)
        turnEndRef.current(sessionId)
    },
    [sessionId],
  )
  useEventSubscription(EventTypes.STREAM_COMPLETED, onStreamCompleted)

  const send = useCallback(
    async (
      content: string,
      context: string | undefined,
      fileIds?: string[],
    ) => {
      onMessageSent()
      if (!sessionId) {
        onSendWithoutSession({ content, context, fileIds })
        return
      }
      try {
        await sendMessage(
          content,
          undefined,
          fileIds,
          undefined,
          undefined,
          context,
        )
      } catch {
        // The hook toasted; give the text back so it can be sent again.
        setRestoreSeed(nextSeed(content))
        setComposerEmpty(false)
      }
    },
    [onMessageSent, onSendWithoutSession, sendMessage, sessionId],
  )

  const sendWithContext = useCallback(
    (content: string, fileIds?: string[]) => {
      const context = buildContext(componentsAlreadyExplained(messages))
      void send(content, context, fileIds)
    },
    [buildContext, messages, send],
  )

  // The first message of a session the parent created for it — once, even
  // when an effect runs twice.
  const takenRef = useRef<PendingSend | null>(null)
  useEffect(() => {
    if (!sessionId || !pending || takenRef.current === pending) return
    takenRef.current = pending
    onPendingTaken()
    void (async () => {
      try {
        await sendMessage(
          pending.content,
          undefined,
          pending.fileIds,
          undefined,
          undefined,
          pending.context,
        )
      } catch {
        onSeed(pending.content)
      }
    })()
  }, [sessionId, pending, onPendingTaken, sendMessage, onSeed])

  // Composer focus collects the html page's context; its text decides whether
  // the starters show. Observed on the textarea, never on an iframe's blur.
  useEffect(() => {
    const textarea = composerRef.current
    if (!textarea) return
    const onFocus = () => onComposerFocus()
    const onInput = () => setComposerEmpty(!textarea.value.trim())
    textarea.addEventListener("focus", onFocus)
    textarea.addEventListener("input", onInput)
    return () => {
      textarea.removeEventListener("focus", onFocus)
      textarea.removeEventListener("input", onInput)
    }
  }, [onComposerFocus])
  useEffect(() => {
    if (effectiveSeed?.text) setComposerEmpty(false)
  }, [effectiveSeed])

  const busy = isStreaming || isPending
  const loading = !!sessionId && messagesQuery.isLoading

  return (
    <ComponentRefChatContext.Provider
      value={{ sessionId: sessionId ?? null, seedComposer: onSeed }}
    >
      {header({ hasMessages: messages.length > 0 })}
      {loading ? (
        <MessagesSkeleton />
      ) : messages.length === 0 && !isStreaming ? (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">
          Ask {agent.name} about what you see here.
        </div>
      ) : (
        <MessageList
          messages={messages}
          streamingEvents={streamingEvents}
          isStreaming={isStreaming}
          onSendAnswer={(content, answersTo) => {
            void sendMessage(content, answersTo).catch(() => undefined)
          }}
          onSendMessage={(content) => {
            void sendMessage(content).catch(() => undefined)
          }}
          conversationModeUi={agent.conversation_mode_ui || "detailed"}
          agentId={agent.id}
          sessionId={sessionId}
        />
      )}
      {composerEmpty && !busy && (
        <PromptStarters
          prompts={starters}
          onSend={(prompt) => {
            // The pills unmount once the turn starts: keep focus in the panel.
            composerRef.current?.focus()
            sendWithContext(prompt)
          }}
        />
      )}
      {chip && (
        // biome-ignore lint/a11y/useKeyWithClickEvents lint/a11y/noStaticElementInteractions: delegation only — the chip's X is a real button; this moves focus to the composer after it unmounts.
        <div
          className="flex px-3 pb-1.5"
          onClick={(event) => {
            if ((event.target as HTMLElement).closest("[data-chip-remove]"))
              window.setTimeout(() => composerRef.current?.focus(), 0)
          }}
        >
          {chip}
        </div>
      )}
      <MessageInput
        ref={composerRef}
        onSend={(content, fileIds) => {
          setComposerEmpty(true)
          sendWithContext(content, fileIds)
        }}
        onStop={stopMessage}
        isStreaming={isStreaming}
        isInterruptPending={isInterruptPending}
        placeholder="Ask about this dashboard"
        agentId={agent.id}
        mode="conversation"
        sessionId={sessionId}
        seed={effectiveSeed}
        compact
      />
    </ComponentRefChatContext.Provider>
  )
}
