import { Button } from "@/components/ui/button"
import { Hammer, MessageCircle } from "lucide-react"

interface ModeSwitchToggleProps {
  mode: string
  onToggle: () => void
  disabled?: boolean
}

export function ModeSwitchToggle({ mode, onToggle, disabled = false }: ModeSwitchToggleProps) {
  const isBuilding = mode === "building"

  return (
    <div className="flex items-center gap-2">
      <Button
        variant={isBuilding ? "outline" : "default"}
        size="sm"
        onClick={onToggle}
        disabled={disabled}
        className={`gap-2 ${
          !isBuilding
            ? "bg-blue-500 hover:bg-blue-600 text-white"
            : "border-orange-500 text-orange-700 dark:text-orange-400 hover:bg-orange-50 dark:hover:bg-orange-950/20"
        }`}
      >
        {isBuilding ? (
          <>
            <Hammer className="h-4 w-4" />
            Building Mode
          </>
        ) : (
          <>
            <MessageCircle className="h-4 w-4" />
            Conversation Mode
          </>
        )}
      </Button>
      <div className="text-xs text-muted-foreground">
        {isBuilding ? (
          <p>Set up capabilities and write scripts</p>
        ) : (
          <p>Execute tasks with pre-built tools</p>
        )}
      </div>
    </div>
  )
}
