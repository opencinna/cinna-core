import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import type { PendingSharePublic } from "@/client"
import { User, Wrench, MessageCircle } from "lucide-react"

interface PendingAgentCardProps {
  share: PendingSharePublic
  onAccept: () => void
  onDecline: () => void
  isLoading?: boolean
}

export function PendingAgentCard({
  share,
  onAccept,
  onDecline,
  isLoading = false,
}: PendingAgentCardProps) {
  const modeLabel = share.share_mode === "builder" ? "Builder Access" : "Conversation Access"
  const modeColor =
    share.share_mode === "builder"
      ? "bg-purple-100 text-purple-800"
      : "bg-blue-100 text-blue-800"

  return (
    <Card className="border-dashed border-2 border-muted-foreground/30 bg-muted/20 h-full flex flex-col">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-lg break-words min-w-0">
            {share.original_agent_name}
          </CardTitle>
          <Badge variant="outline" className="shrink-0">
            Pending
          </Badge>
        </div>
        {share.original_agent_description && (
          <p className="text-sm text-muted-foreground line-clamp-2">
            {share.original_agent_description}
          </p>
        )}
      </CardHeader>

      <CardContent className="pb-3 flex-1">
        <div className="flex items-center text-sm text-muted-foreground mb-2">
          <User className="h-4 w-4 mr-2 shrink-0" />
          <span className="truncate">Shared by: {share.shared_by_email}</span>
        </div>

        <Tooltip>
          <TooltipTrigger asChild>
            <Badge className={modeColor}>
              {share.share_mode === "builder" ? (
                <Wrench className="h-3 w-3 mr-1" />
              ) : (
                <MessageCircle className="h-3 w-3 mr-1" />
              )}
              {modeLabel}
            </Badge>
          </TooltipTrigger>
          <TooltipContent>
            {share.share_mode === "builder"
              ? "Full access: You can modify prompts, scripts, and settings"
              : "Conversation access: You can use the agent but not modify its configuration"}
          </TooltipContent>
        </Tooltip>
      </CardContent>

      <CardFooter className="flex justify-end gap-2 pt-3 border-t">
        <Button
          variant="outline"
          size="sm"
          onClick={onDecline}
          disabled={isLoading}
        >
          Decline
        </Button>
        <Button size="sm" onClick={onAccept} disabled={isLoading}>
          Accept
        </Button>
      </CardFooter>
    </Card>
  )
}
