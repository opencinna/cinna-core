import { formatDistanceToNow } from "date-fns"

interface RelativeTimeProps {
  timestamp: string | Date
  fallback?: string
  className?: string
}

/**
 * Component to display relative time (e.g., "2 minutes ago", "3 hours ago")
 * with robust timestamp handling and graceful error fallback.
 */
export function RelativeTime({
  timestamp,
  fallback = "recently",
  className = ""
}: RelativeTimeProps) {
  const formattedTime = (() => {
    try {
      // Handle timestamp - it might already have 'Z'
      const timestampStr = typeof timestamp === 'string'
        ? (timestamp.endsWith('Z') ? timestamp : timestamp + 'Z')
        : timestamp
      const date = new Date(timestampStr)
      return !isNaN(date.getTime())
        ? formatDistanceToNow(date, { addSuffix: true })
        : fallback
    } catch {
      return fallback
    }
  })()

  return <span className={className}>{formattedTime}</span>
}
