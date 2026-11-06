import { Palette } from "lucide-react"
import { useMemo, useState } from "react"

import type { BrandThemeDraftPublic } from "@/client"
import { parseBrandThemePreview } from "@/components/BrandTheme/brandTheme"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { BrandThemeSwatch } from "./BrandThemeSwatch"
import { useIsUnpublishing } from "./brandThemeQueries"
import { UnpublishBrandThemeDialog } from "./UnpublishBrandThemeDialog"

interface PublishedThemeCardProps {
  /** The row with `status === "published"`, or null when none is. */
  published: BrandThemeDraftPublic | null
  isLoading: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
  onPreview: (draft: BrandThemeDraftPublic) => void
}

/**
 * S1 — is a theme live, which one, who put it there, and how to take it down.
 * Read from the same list as the Drafts card, so the two never disagree.
 */
export function PublishedThemeCard({
  published,
  isLoading,
  isError,
  error,
  onRetry,
  onPreview,
}: PublishedThemeCardProps) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <Palette className="h-5 w-5 shrink-0" />
          Published theme
        </CardTitle>
        <CardDescription>
          What every sign-in page and Cinna Desktop on this server shows
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isError ? (
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load brand themes"
            onRetry={onRetry}
          />
        ) : isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-[48px] w-full rounded-md" />
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : published ? (
          <PublishedTheme theme={published} onPreview={onPreview} />
        ) : (
          <p className="text-sm text-muted-foreground">
            No theme is published, so this server shows stock Cinna. Publish one
            of the drafts.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function PublishedTheme({
  theme,
  onPreview,
}: {
  theme: BrandThemeDraftPublic
  onPreview: (draft: BrandThemeDraftPublic) => void
}) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const isUnpublishing = useIsUnpublishing()
  const look = useMemo(
    () => parseBrandThemePreview(theme.document),
    [theme.document],
  )
  const name = theme.name || "Untitled theme"

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <BrandThemeSwatch look={look} className="h-8 w-8" />
        <span className="min-w-0 truncate text-sm font-medium">{name}</span>
        {theme.revision != null && (
          <Badge variant="secondary" className="h-5 shrink-0">
            rev {theme.revision}
          </Badge>
        )}
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
        <dt className="text-xs leading-5 text-muted-foreground">
          Published by
        </dt>
        <dd className="min-w-0 truncate text-sm">
          {theme.published_by_email || "Unknown"}
        </dd>
        <dt className="text-xs leading-5 text-muted-foreground">Published</dt>
        <dd className="text-sm">
          {theme.published_at ? (
            <RelativeTime timestamp={theme.published_at} showTooltip />
          ) : (
            "Unknown"
          )}
        </dd>
      </dl>

      <div className="flex gap-2">
        <Button variant="outline" size="sm" onClick={() => onPreview(theme)}>
          Preview
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={isUnpublishing}
          onClick={() => setConfirmOpen(true)}
        >
          Unpublish
        </Button>
      </div>

      <UnpublishBrandThemeDialog
        name={name}
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
      />
    </div>
  )
}
