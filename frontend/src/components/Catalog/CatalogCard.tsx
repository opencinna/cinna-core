/**
 * CatalogCard — single bundle in the catalog grid.
 *
 * Surfaces display_name, description, publisher (name + email, falling
 * back to the truncated handle), latest version, and either a
 * "Quick Install" button (one-click install with default selections) or
 * an "Open" link when the user already has an install.
 *
 * Clicking the card body (outside the action button) navigates to the
 * install page for un-installed bundles, or the agent detail page for
 * installed ones.
 */
import { useNavigate } from "@tanstack/react-router"
import {
  Bot,
  Download,
  ExternalLink,
  Globe,
  Loader2,
  Lock,
  Users,
} from "lucide-react"
import type { MouseEvent, ReactNode } from "react"

import type { CatalogEntryPublic } from "@/client"
import { useQuickInstall } from "@/components/Install/useQuickInstall"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

interface CatalogCardProps {
  entry: CatalogEntryPublic
}

const VISIBILITY_ICONS: Record<string, ReactNode> = {
  public: <Globe className="h-3 w-3" />,
  users: <Users className="h-3 w-3" />,
  private: <Lock className="h-3 w-3" />,
}

export function CatalogCard({ entry }: CatalogCardProps) {
  const navigate = useNavigate()
  const visibilityIcon = VISIBILITY_ICONS[entry.visibility] ?? null
  const quickInstall = useQuickInstall(entry.bundle_id)

  const handleCardClick = () => {
    if (quickInstall.isPending) return
    if (entry.is_installed && entry.user_install_id) {
      navigate({
        to: "/agent/$agentId",
        params: { agentId: entry.user_install_id },
      })
    } else {
      navigate({
        to: "/catalog/agents/install/$bundleId",
        params: { bundleId: entry.bundle_id },
      })
    }
  }

  const handleQuickInstall = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation()
    quickInstall.mutate()
  }

  const handleOpen = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation()
    if (entry.user_install_id) {
      navigate({
        to: "/agent/$agentId",
        params: { agentId: entry.user_install_id },
      })
    }
  }

  return (
    <Card
      className="flex flex-col h-full cursor-pointer transition-colors hover:bg-accent/30 has-[button:hover]:bg-transparent has-[a:hover]:bg-transparent"
      onClick={handleCardClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          handleCardClick()
        }
      }}
    >
      <CardHeader className="pb-2">
        <div className="flex items-start gap-3">
          <div className="rounded-lg p-2 bg-muted shrink-0">
            <Bot className="h-5 w-5 text-muted-foreground" />
          </div>
          <div className="flex-1 min-w-0">
            <CardTitle className="text-lg break-words leading-tight">
              {entry.display_name}
            </CardTitle>
            <p className="text-xs text-muted-foreground mt-1 truncate">
              by{" "}
              {entry.publisher_name ||
                entry.publisher_email ||
                entry.publisher_handle ||
                "unknown publisher"}
            </p>
            {entry.publisher_name && entry.publisher_email && (
              <p className="text-xs text-muted-foreground truncate">
                {entry.publisher_email}
              </p>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-0 flex-1 min-h-0 space-y-2">
        {entry.description && (
          <CardDescription className="line-clamp-3">
            {entry.description}
          </CardDescription>
        )}
        <div className="flex items-center gap-1.5 flex-wrap text-xs text-muted-foreground">
          <Badge variant="outline" className="gap-1 font-normal">
            {visibilityIcon}
            {entry.visibility}
          </Badge>
          {entry.latest_version ? (
            <Badge variant="outline" className="font-normal">
              v{entry.latest_version}
            </Badge>
          ) : entry.latest_revision_number !== null ? (
            <Badge variant="outline" className="font-normal">
              rev {entry.latest_revision_number}
            </Badge>
          ) : null}
        </div>
        <code
          className="block font-mono text-[11px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded truncate"
          title={entry.bundle_id}
        >
          {entry.bundle_id}
        </code>
      </CardContent>
      <CardFooter className="pt-2">
        {entry.is_installed && entry.user_install_id ? (
          <Button
            variant="outline"
            className="w-full"
            onClick={handleOpen}
          >
            <ExternalLink className="h-4 w-4 mr-2" />
            Open
          </Button>
        ) : (
          <Button
            className="group relative w-full overflow-hidden shadow-sm transition-all duration-200 hover:shadow-md hover:ring-2 hover:ring-primary/30 hover:ring-offset-1"
            onClick={handleQuickInstall}
            disabled={quickInstall.isPending}
          >
            <span
              aria-hidden
              className="pointer-events-none absolute inset-y-0 left-0 w-1/2 -translate-x-full skew-x-12 bg-gradient-to-r from-transparent via-white/25 to-transparent transition-transform duration-700 ease-out group-hover:translate-x-[200%]"
            />
            {quickInstall.isPending ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Download className="h-4 w-4 mr-2" />
            )}
            {quickInstall.isPending ? "Installing…" : "Quick Install"}
          </Button>
        )}
      </CardFooter>
    </Card>
  )
}
