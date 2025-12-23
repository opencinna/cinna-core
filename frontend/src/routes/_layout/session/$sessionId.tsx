import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"

import { SessionsService, MessagesService } from "@/client"
import { ChatHeader } from "@/components/Chat/ChatHeader"
import { MessageList } from "@/components/Chat/MessageList"
import { MessageInput } from "@/components/Chat/MessageInput"
import PendingItems from "@/components/Pending/PendingItems"
import useCustomToast from "@/hooks/useCustomToast"

export const Route = createFileRoute("/_layout/session/$sessionId")({
  component: ChatInterface,
})

function ChatInterface() {
  const { sessionId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const {
    data: session,
    isLoading: sessionLoading,
    error: sessionError,
  } = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => SessionsService.getSession({ id: sessionId }),
    enabled: !!sessionId,
    refetchInterval: 10000, // Poll for status updates every 10s
  })

  const {
    data: messagesData,
    isLoading: messagesLoading,
  } = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => MessagesService.getMessages({ sessionId, offset: 0, limit: 100 }),
    enabled: !!sessionId,
  })

  const switchModeMutation = useMutation({
    mutationFn: (newMode: string) =>
      SessionsService.switchSessionMode({ id: sessionId, newMode }),
    onSuccess: (updatedSession) => {
      showSuccessToast(`Switched to ${updatedSession.mode} mode`)
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to switch mode")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
    },
  })

  const sendMessageMutation = useMutation({
    mutationFn: (content: string) =>
      MessagesService.sendMessage({
        sessionId,
        requestBody: { content },
      }),
    onSuccess: () => {
      // Refetch messages to get the agent's response
      queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to send message")
    },
  })

  const handleModeSwitch = () => {
    if (!session) return
    const newMode = session.mode === "building" ? "conversation" : "building"
    switchModeMutation.mutate(newMode)
  }

  const handleSendMessage = (content: string) => {
    sendMessageMutation.mutate(content)
  }

  const handleBack = () => {
    navigate({ to: "/sessions" })
  }

  if (sessionLoading || messagesLoading) {
    return <PendingItems />
  }

  if (sessionError || !session) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive mb-4">Error loading session</p>
        <button onClick={handleBack} className="text-primary hover:underline">
          Back to sessions
        </button>
      </div>
    )
  }

  const messages = messagesData?.data || []

  // Check if session is in error state
  if (session.status === "error") {
    return (
      <div className="flex flex-col h-screen">
        <ChatHeader session={session} onModeSwitch={handleModeSwitch} onBack={handleBack} />
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center space-y-4">
            <p className="text-destructive font-semibold">Session encountered an error</p>
            <p className="text-muted-foreground">
              Please check the environment status or create a new session.
            </p>
          </div>
        </div>
      </div>
    )
  }

  const isDisabled = sendMessageMutation.isPending || session.status !== "active"

  return (
    <div className="flex flex-col h-screen">
      <ChatHeader session={session} onModeSwitch={handleModeSwitch} onBack={handleBack} />
      <MessageList messages={messages} isLoading={messagesLoading} />
      <MessageInput
        onSend={handleSendMessage}
        disabled={isDisabled}
        placeholder={
          sendMessageMutation.isPending
            ? "Sending message..."
            : session.status !== "active"
            ? "Session is not active"
            : "Type your message..."
        }
      />
    </div>
  )
}
