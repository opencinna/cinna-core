import { Link } from "@tanstack/react-router"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import type { SessionPublic } from "@/client"
import { SessionModeBadge } from "./SessionModeBadge"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { getColorPreset } from "@/utils/colorPresets"
import { AnimatedPlaceholder } from "@/components/Common/AnimatedPlaceholder"

interface SessionCardProps {
  session: SessionPublic
  agentName?: string
  agentColorPreset?: string | null
}

export function SessionCard({ session, agentName, agentColorPreset }: SessionCardProps) {
  const getStatusVariant = (status: string) => {
    switch (status) {
      case "active":
        return "default"
      case "paused":
        return "secondary"
      case "completed":
        return "outline"
      case "error":
        return "destructive"
      default:
        return "secondary"
    }
  }

  const colorPreset = getColorPreset(agentColorPreset)

  return (
    <Link
      to="/session/$sessionId"
      params={{ sessionId: session.id }}
      className="block"
    >
      <Card className="p-4 hover:shadow-md hover:-translate-y-0.5 transition-all duration-200 cursor-pointer">
        <div className="space-y-3">
          <div className="flex items-start justify-between gap-2">
            <h3 className="font-semibold break-words flex-1">
              {session.title ? session.title : <AnimatedPlaceholder />}
            </h3>
            <SessionModeBadge mode={session.mode} />
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            {agentName && (
              <span className={`text-xs px-2 py-1 rounded-md ${colorPreset.badgeBg} ${colorPreset.badgeText}`}>
                {agentName}
              </span>
            )}
            <Badge variant={getStatusVariant(session.status)} className="text-xs">
              {session.status}
            </Badge>
          </div>

          <div className="text-sm text-muted-foreground">
            {session.last_message_at ? (
              <p>
                Last message <RelativeTime timestamp={session.last_message_at} />
              </p>
            ) : (
              <p>No messages yet</p>
            )}
          </div>
        </div>
      </Card>
    </Link>
  )
}
