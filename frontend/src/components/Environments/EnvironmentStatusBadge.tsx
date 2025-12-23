import { Badge } from "@/components/ui/badge"

interface EnvironmentStatusBadgeProps {
  status: string
}

export function EnvironmentStatusBadge({ status }: EnvironmentStatusBadgeProps) {
  const getStatusVariant = (status: string) => {
    switch (status) {
      case "running":
        return "default" // Green
      case "stopped":
        return "secondary" // Gray
      case "starting":
        return "outline" // Yellow/orange with animation
      case "error":
        return "destructive" // Red
      case "deprecated":
        return "secondary" // Muted gray
      default:
        return "secondary"
    }
  }

  const getStatusLabel = (status: string) => {
    switch (status) {
      case "running":
        return "Running"
      case "stopped":
        return "Stopped"
      case "starting":
        return "Starting..."
      case "error":
        return "Error"
      case "deprecated":
        return "Deprecated"
      default:
        return status
    }
  }

  return (
    <Badge
      variant={getStatusVariant(status)}
      className={status === "starting" ? "animate-pulse" : ""}
    >
      {getStatusLabel(status)}
    </Badge>
  )
}
