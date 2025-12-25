import { useQuery } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { ArrowLeft, EllipsisVertical } from "lucide-react"

import { SessionsService, MessagesService } from "@/client"
import { MessageList } from "@/components/Chat/MessageList"
import { MessageInput } from "@/components/Chat/MessageInput"
import EditSession from "@/components/Sessions/EditSession"
import DeleteSession from "@/components/Sessions/DeleteSession"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import PendingItems from "@/components/Pending/PendingItems"
import useCustomToast from "@/hooks/useCustomToast"
import { useMessageStream } from "@/hooks/useMessageStream"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/session/$sessionId")({
  component: ChatInterface,
})

function ChatInterface() {
  const { sessionId } = Route.useParams()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { setHeaderContent } = usePageHeader()
  const [menuOpen, setMenuOpen] = useState(false)

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

  const { sendMessage, isStreaming, streamingEvents } = useMessageStream({
    sessionId,
    onSuccess: () => {
      // Messages are already refreshed by the hook
    },
    onError: (error) => {
      showErrorToast(error.message || "Failed to send message")
    },
  })

  const handleSendMessage = async (content: string) => {
    await sendMessage(content)
  }

  const handleBack = () => {
    navigate({ to: "/sessions" })
  }

  const handleDeleteSuccess = () => {
    navigate({ to: "/sessions" })
  }

  // Update header when session loads
  useEffect(() => {
    if (session) {
      const isBuilding = session.mode === "building"
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <h1 className="text-base font-semibold truncate">
                {session.title || "Untitled Session"}
              </h1>
              <p className="text-xs text-muted-foreground">
                <span className={`inline-block w-2 h-2 rounded-full mr-1.5 ${
                  isBuilding ? "bg-orange-500" : "bg-blue-500"
                }`} />
                {isBuilding ? "Building Mode" : "Conversation Mode"}
              </p>
            </div>
          </div>
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="shrink-0">
                <EllipsisVertical className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <EditSession session={session} onSuccess={() => setMenuOpen(false)} />
              <DeleteSession
                id={session.id}
                onSuccess={handleDeleteSuccess}
              />
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [session, setHeaderContent, menuOpen])

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
      <div className="flex flex-col h-full min-h-0">
        <div className="flex-1 flex items-center justify-center min-h-0">
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

  return (
    <div className="flex flex-col h-full min-h-0">
      <MessageList
        messages={messages}
        isLoading={messagesLoading}
        streamingEvents={streamingEvents}
        isStreaming={isStreaming}
      />
      <MessageInput
        onSend={handleSendMessage}
        sendDisabled={isStreaming}
        placeholder={
          isStreaming
            ? "Agent is responding..."
            : "Type your message..."
        }
      />
    </div>
  )
}
