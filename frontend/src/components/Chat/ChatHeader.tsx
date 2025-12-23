import { Button } from "@/components/ui/button"
import { ArrowLeft } from "lucide-react"
import type { SessionPublic } from "@/client"
import { ModeSwitchToggle } from "./ModeSwitchToggle"

interface ChatHeaderProps {
  session: SessionPublic
  onModeSwitch: () => void
  onBack: () => void
}

export function ChatHeader({ session, onModeSwitch, onBack }: ChatHeaderProps) {
  const isBuilding = session.mode === "building"

  return (
    <div
      className={`border-b p-4 ${
        isBuilding
          ? "bg-gradient-to-r from-orange-50 to-orange-100 dark:from-orange-950/20 dark:to-orange-900/20"
          : "bg-gradient-to-r from-blue-50 to-blue-100 dark:from-blue-950/20 dark:to-blue-900/20"
      }`}
    >
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={onBack}>
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div>
            <h1 className="text-xl font-bold break-words">
              {session.title || "Untitled Session"}
            </h1>
            <p className="text-sm text-muted-foreground">Session ID: {session.id.slice(0, 8)}...</p>
          </div>
        </div>

        <ModeSwitchToggle mode={session.mode} onToggle={onModeSwitch} />
      </div>
    </div>
  )
}
