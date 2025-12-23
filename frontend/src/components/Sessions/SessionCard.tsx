import { Link } from "@tanstack/react-router"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import type { SessionPublic } from "@/client"
import { SessionModeBadge } from "./SessionModeBadge"
import { formatDistanceToNow } from "date-fns"

interface SessionCardProps {
  session: SessionPublic
  agentName?: string
}

export function SessionCard({ session, agentName }: SessionCardProps) {
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
              {session.title || "Untitled Session"}
            </h3>
            <SessionModeBadge mode={session.mode} />
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            {agentName && (
              <Badge variant="outline" className="text-xs">
                {agentName}
              </Badge>
            )}
            <Badge variant={getStatusVariant(session.status)} className="text-xs">
              {session.status}
            </Badge>
          </div>

          <div className="text-sm text-muted-foreground">
            {session.last_message_at ? (
              <p>
                Last message{" "}
                {formatDistanceToNow(new Date(session.last_message_at), {
                  addSuffix: true,
                })}
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
