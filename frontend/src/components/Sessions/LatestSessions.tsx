import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { SessionsService } from "@/client"
import type { SessionPublicExtended } from "@/client"
import { MessageSquare, Clock, Wrench, MessageCircle } from "lucide-react"
import { AnimatedPlaceholder } from "@/components/Common/AnimatedPlaceholder"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { getColorPreset } from "@/utils/colorPresets"

interface LatestSessionsProps {
  limit?: number
}

export function LatestSessions({ limit = 8 }: LatestSessionsProps) {
  const navigate = useNavigate()

  const { data, isLoading } = useQuery({
    queryKey: ["sessions", "latest", limit],
    queryFn: () =>
      SessionsService.listSessions({
        skip: 0,
        limit,
        orderBy: "last_message_at",
        orderDesc: true,
      }),
  })

  const sessions = data?.data || []

  // Don't render anything if no sessions (including loading state)
  if (isLoading || sessions.length === 0) {
    return null
  }

  const handleSessionClick = (sessionId: string) => {
    navigate({
      to: "/session/$sessionId",
      params: { sessionId },
    })
  }

  return (
    <div className="w-full">
      <div className="flex items-center gap-2 mb-2 px-1">
        <MessageSquare className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-sm font-medium text-muted-foreground">Recent Conversations</h2>
      </div>

      <div className="space-y-0.5">
        {sessions.map((session: SessionPublicExtended) => {
          const colorPreset = getColorPreset(session.agent_ui_color_preset)
          return (
            <button
              key={session.id}
              onClick={() => handleSessionClick(session.id)}
              className="w-full text-left px-3 py-2 rounded-md hover:bg-accent transition-colors group"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    {session.mode === "building" ? (
                      <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                    ) : (
                      <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                    )}
                    <p className="text-sm text-foreground truncate">
                      {session.title ? session.title : <AnimatedPlaceholder className="text-xs" />}
                    </p>
                    {session.agent_name && (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${colorPreset.badgeBg} ${colorPreset.badgeText} shrink-0`}>
                        {session.agent_name}
                      </span>
                    )}
                  </div>
                </div>

              <div className="flex items-center gap-1 text-xs text-muted-foreground shrink-0">
                <Clock className="h-3 w-3" />
                <RelativeTime timestamp={session.last_message_at || session.created_at} />
              </div>
            </div>
          </button>
          )
        })}
      </div>
    </div>
  )
}
