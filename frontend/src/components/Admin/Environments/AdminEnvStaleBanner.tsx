import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { AlertTriangle } from "lucide-react"

interface AdminEnvStaleBannerProps {
  staleCount: number
  totalCount: number
  onSelectAllStale: () => void
}

export function AdminEnvStaleBanner({
  staleCount,
  totalCount,
  onSelectAllStale,
}: AdminEnvStaleBannerProps) {
  if (staleCount === 0) return null

  return (
    <Alert className="border-orange-300 bg-orange-50 text-orange-900 dark:border-orange-700 dark:bg-orange-950 dark:text-orange-100">
      <AlertTriangle className="h-4 w-4 text-orange-500" />
      <AlertDescription className="flex items-center justify-between gap-4">
        <span>
          <strong>{staleCount}</strong> of{" "}
          <strong>{totalCount}</strong> environments are behind the current
          template image.
        </span>
        <Button
          size="sm"
          variant="outline"
          onClick={onSelectAllStale}
          className="shrink-0 border-orange-400 text-orange-800 hover:bg-orange-100 dark:border-orange-600 dark:text-orange-200 dark:hover:bg-orange-900"
        >
          Select all stale
        </Button>
      </AlertDescription>
    </Alert>
  )
}
