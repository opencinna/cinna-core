import { useNavigate, Link } from "@tanstack/react-router"
import type { SessionPublicExtended } from "@/client"
import { MessageCircle, Wrench, Clock, Bot } from "lucide-react"
import { AnimatedPlaceholder } from "@/components/Common/AnimatedPlaceholder"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { getColorPreset } from "@/utils/colorPresets"

interface AgentSessionsGroupProps {
  agentId: string
  agentName: string
  agentColorPreset: string | null
  sessions: SessionPublicExtended[]
  maxSessions?: number
}

export function AgentSessionsGroup({
  agentId,
  agentName,
  agentColorPreset,
  sessions,
  maxSessions = 10,
}: AgentSessionsGroupProps) {
  const navigate = useNavigate()
  const colorPreset = getColorPreset(agentColorPreset)

  const displaySessions = sessions.slice(0, maxSessions)
  const hasMore = sessions.length > maxSessions

  const handleSessionClick = (sessionId: string) => {
    navigate({
      to: "/session/$sessionId",
      params: { sessionId },
      search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined },
    })
  }

  return (
    <div className="rounded-lg bg-muted/30 p-4">
      {/* Agent Header */}
      <div className="mb-3 flex items-center gap-2">
        <div className={`rounded-md p-1.5 ${colorPreset.iconBg}`}>
          <Bot className={`h-4 w-4 ${colorPreset.iconText}`} />
        </div>
        <h3 className="text-base font-semibold text-foreground">
          {agentName}
        </h3>
      </div>

      {/* Sessions List */}
      <div className="space-y-0.5">
        {displaySessions.map((session: SessionPublicExtended) => (
          <button
            key={session.id}
            onClick={() => handleSessionClick(session.id)}
            className="w-full text-left px-3 py-2 rounded-md hover:bg-accent transition-colors group"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  {session.mode === "building" ? (
                    <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                  ) : (
                    <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                  )}
                  <p className="text-sm text-foreground truncate">
                    {session.title ? session.title : <AnimatedPlaceholder className="text-xs" />}
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-1 text-xs text-muted-foreground shrink-0">
                <Clock className="h-3 w-3" />
                <RelativeTime timestamp={session.last_message_at || session.created_at} />
              </div>
            </div>
          </button>
        ))}
      </div>

      {/* Show All Link */}
      {hasMore && (
        <Link
          to="/sessions/agent/$agentId"
          params={{ agentId }}
          className="block text-center text-xs mt-3 pt-3 border-t text-muted-foreground hover:text-foreground hover:underline transition-colors"
        >
          Show all sessions ({sessions.length})
        </Link>
      )}
    </div>
  )
}
