import { Link } from "@tanstack/react-router"
import { ArrowLeft, Compass, Home } from "lucide-react"

import { Button } from "@/components/ui/button"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"

interface NotFoundProps {
  /** Headline shown under the 404 marker. */
  title?: string
  /** Supporting copy explaining what happened. */
  message?: string
  /**
   * Render compactly inside the app layout's main content area (no full
   * viewport height) instead of as a standalone full-screen page. Use this
   * for entity detail pages where the sidebar/header are already present.
   */
  inline?: boolean
  /** Where the "Go back" button lands when there is no navigation history. */
  fallbackPath?: string
}

/**
 * Styled 404 / missing-resource screen.
 *
 * Used in three places:
 * - the router's `defaultNotFoundComponent` (unmatched URLs, full screen)
 * - the root route's `notFoundComponent`
 * - entity detail pages, with `inline` + a tailored title/message, when the
 *   requested resource was deleted or belongs to another user.
 */
const NotFound = ({
  title = "Page not found",
  message = "The page you're looking for doesn't exist, was removed, or you don't have access to it.",
  inline = false,
  fallbackPath = "/",
}: NotFoundProps) => {
  const { goBack } = useNavigationHistory()

  return (
    <div
      className={`flex w-full flex-col items-center justify-center p-6 ${
        inline ? "flex-1 min-h-[60vh] py-16" : "min-h-screen"
      }`}
      data-testid="not-found"
    >
      <div className="flex w-full max-w-md flex-col items-center text-center">
        <div className="mb-6 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
          <Compass className="h-8 w-8 text-muted-foreground" />
        </div>

        <span className="mb-2 text-sm font-semibold uppercase tracking-widest text-muted-foreground">
          404
        </span>
        <h1 className="mb-2 text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="mb-8 text-muted-foreground">{message}</p>

        <div className="flex flex-wrap items-center justify-center gap-3">
          <Button variant="outline" onClick={() => goBack(fallbackPath)}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Go back
          </Button>
          <Link to="/">
            <Button>
              <Home className="mr-2 h-4 w-4" />
              Go home
            </Button>
          </Link>
        </div>
      </div>
    </div>
  )
}

export default NotFound
