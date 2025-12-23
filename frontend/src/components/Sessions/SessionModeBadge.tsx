import { Badge } from "@/components/ui/badge"
import { Hammer, MessageCircle } from "lucide-react"

interface SessionModeBadgeProps {
  mode: string
}

export function SessionModeBadge({ mode }: SessionModeBadgeProps) {
  const isBuilding = mode === "building"

  return (
    <Badge
      variant={isBuilding ? "outline" : "default"}
      className={`gap-1 ${
        isBuilding
          ? "border-orange-500 text-orange-700 dark:text-orange-400"
          : "border-blue-500 bg-blue-500 text-white"
      }`}
    >
      {isBuilding ? (
        <>
          <Hammer className="h-3 w-3" />
          Building Mode
        </>
      ) : (
        <>
          <MessageCircle className="h-3 w-3" />
          Conversation Mode
        </>
      )}
    </Badge>
  )
}
