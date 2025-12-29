import { useMemo } from "react"
import { useNavigate } from "@tanstack/react-router"
import type { SessionPublicExtended } from "@/client"
import { MessageCircle, Wrench, Clock } from "lucide-react"
import { format, startOfMonth } from "date-fns"
import { AnimatedPlaceholder } from "@/components/Common/AnimatedPlaceholder"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { getColorPreset } from "@/utils/colorPresets"

interface AgentConversationsProps {
  sessions: SessionPublicExtended[]
  agentName: string
  agentColorPreset: string | null
}

interface MonthGroup {
  monthKey: string
  monthLabel: string
  sessions: SessionPublicExtended[]
}

export function AgentConversations({
  sessions,
  agentName,
  agentColorPreset,
}: AgentConversationsProps) {
  const navigate = useNavigate()
  const colorPreset = getColorPreset(agentColorPreset)

  // Group sessions by month based on last_message_at
  const monthGroups = useMemo(() => {
    const groups = new Map<string, MonthGroup>()

    // Sort sessions by last_message_at descending (most recent first)
    const sortedSessions = [...sessions].sort((a, b) => {
      const dateA = new Date(a.last_message_at || a.created_at)
      const dateB = new Date(b.last_message_at || b.created_at)
      return dateB.getTime() - dateA.getTime()
    })

    sortedSessions.forEach((session) => {
      try {
        const timestamp = session.last_message_at || session.created_at
        const timestampStr = typeof timestamp === 'string'
          ? (timestamp.endsWith('Z') ? timestamp : timestamp + 'Z')
          : timestamp
        const date = new Date(timestampStr)

        if (!isNaN(date.getTime())) {
          const monthStart = startOfMonth(date)
          const monthKey = format(monthStart, "yyyy-MM")
          const monthLabel = format(monthStart, "MMMM yyyy")

          if (!groups.has(monthKey)) {
            groups.set(monthKey, {
              monthKey,
              monthLabel,
              sessions: [],
            })
          }

          groups.get(monthKey)!.sessions.push(session)
        }
      } catch {
        // Skip sessions with invalid dates
      }
    })

    // Convert to array and sort by month (most recent first)
    return Array.from(groups.values()).sort((a, b) => {
      return b.monthKey.localeCompare(a.monthKey)
    })
  }, [sessions])

  const handleSessionClick = (sessionId: string) => {
    navigate({
      to: "/session/$sessionId",
      params: { sessionId },
    })
  }

  if (sessions.length === 0) {
    return (
      <div className="text-center py-12 border-2 border-dashed rounded-lg">
        <p className="text-muted-foreground">No conversations yet with {agentName}</p>
      </div>
    )
  }

  return (
    <div className="space-y-8">
      {monthGroups.map((group) => (
        <div key={group.monthKey}>
          {/* Month Header */}
          <div className="mb-3 px-1">
            <h2 className={`text-sm font-semibold ${colorPreset.badgeText}`}>
              {group.monthLabel}
            </h2>
          </div>

          {/* Sessions for this month */}
          <div className="space-y-0.5">
            {group.sessions.map((session: SessionPublicExtended) => (
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
        </div>
      ))}
    </div>
  )
}
