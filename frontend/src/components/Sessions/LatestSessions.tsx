import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { SessionsService } from "@/client"
import type { SessionPublicExtended } from "@/client"
import { Clock, Wrench, MessageCircle } from "lucide-react"
import { AnimatedPlaceholder } from "@/components/Common/AnimatedPlaceholder"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { getColorPreset } from "@/utils/colorPresets"
import useWorkspace from "@/hooks/useWorkspace"

interface LatestSessionsProps {
  limit?: number
}

export function LatestSessions({ limit = 8 }: LatestSessionsProps) {
  const navigate = useNavigate()
  const { activeWorkspaceId } = useWorkspace()

  const { data, isLoading } = useQuery({
    queryKey: ["sessions", "latest", limit, activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, , limitValue, workspaceId] = queryKey
      return SessionsService.listSessions({
        skip: 0,
        limit: limitValue as number,
        orderBy: "last_message_at",
        orderDesc: true,
        userWorkspaceId: (workspaceId ?? "") as string,
      })
    },
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
      search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined },
    })
  }

  return (
    <div className="w-full">
      <div className="space-y-0.5">
        {sessions.map((session: SessionPublicExtended) => {
          const colorPreset = getColorPreset(session.agent_ui_color_preset)
          return (
            <button
              key={session.id}
              onClick={() => handleSessionClick(session.id)}
              className="w-full text-left px-3 py-2 rounded-md hover:bg-gradient-to-r hover:from-accent/50 hover:to-accent/30 transition-all group"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 min-w-0">
                    {session.mode === "building" ? (
                      <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
                    ) : (
                      <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
                    )}
                    <p className="text-sm text-foreground truncate min-w-0">
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
