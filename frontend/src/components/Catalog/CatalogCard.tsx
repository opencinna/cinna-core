/**
 * CatalogCard — single bundle in the catalog grid.
 *
 * Surfaces display_name, description, publisher handle, install count,
 * latest revision number, and either an "Install" button or an "Open"
 * link when the user already has an install.
 */
import { Link } from "@tanstack/react-router"
import { Bot, Download, ExternalLink, Lock, Users, Globe } from "lucide-react"
import type { ReactNode } from "react"

import type { CatalogEntryPublic } from "@/client"
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
  const visibilityIcon = VISIBILITY_ICONS[entry.visibility] ?? null

  return (
    <Card className="flex flex-col h-full">
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
              by {entry.publisher_handle ?? "unknown publisher"}
            </p>
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
          {entry.latest_revision_number !== null && (
            <Badge variant="outline" className="font-normal">
              v{entry.latest_revision_number}
            </Badge>
          )}
          {entry.visibility === "public" && (
            <Badge variant="outline" className="font-normal">
              {entry.install_count} install{entry.install_count === 1 ? "" : "s"}
            </Badge>
          )}
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
          <Button asChild variant="outline" className="w-full">
            <Link to="/agent/$agentId" params={{ agentId: entry.user_install_id }}>
              <ExternalLink className="h-4 w-4 mr-2" />
              Open
            </Link>
          </Button>
        ) : (
          <Button asChild className="w-full">
            <Link to="/install/$bundleId" params={{ bundleId: entry.bundle_id }}>
              <Download className="h-4 w-4 mr-2" />
              Install
            </Link>
          </Button>
        )}
      </CardFooter>
    </Card>
  )
}
