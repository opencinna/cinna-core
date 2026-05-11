/**
 * InstallAgentHeaderCard — left-column sticky header on the install page.
 *
 * Shows the bundle identity (display name, version, publisher), the
 * description, and a compact summary of required credentials so the
 * user always knows what they're installing while scrolling the form.
 */
import { Bot, Globe, Lock, Users } from "lucide-react"
import type { ReactNode } from "react"

import type {
  CatalogEntryPublic,
  InstallContextSpec,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

interface InstallAgentHeaderCardProps {
  entry: CatalogEntryPublic
  serviceSpecs: InstallContextSpec[]
}

const VISIBILITY_ICONS: Record<string, ReactNode> = {
  public: <Globe className="h-3.5 w-3.5" />,
  users: <Users className="h-3.5 w-3.5" />,
  private: <Lock className="h-3.5 w-3.5" />,
}

export function InstallAgentHeaderCard({
  entry,
  serviceSpecs,
}: InstallAgentHeaderCardProps) {
  return (
    <Card className="lg:sticky lg:top-4">
      <CardHeader>
        <div className="flex items-start gap-3">
          <div className="rounded-lg p-2 bg-muted shrink-0">
            <Bot className="h-5 w-5 text-muted-foreground" />
          </div>
          <div className="flex-1 min-w-0">
            <CardTitle className="text-xl break-words">
              {entry.display_name}
            </CardTitle>
            <p className="text-xs text-muted-foreground mt-1">
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
      <CardContent className="space-y-4">
        {entry.description && (
          <CardDescription className="leading-relaxed text-sm">
            {entry.description}
          </CardDescription>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="gap-1 font-normal">
            {VISIBILITY_ICONS[entry.visibility]}
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

        {serviceSpecs.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground mb-2">
              Required credentials
            </p>
            <ul className="space-y-1">
              {serviceSpecs.map((spec) => (
                <li
                  key={spec.name}
                  className="text-sm flex items-center gap-2"
                >
                  <Badge variant="secondary" className="text-xs">
                    {spec.type}
                  </Badge>
                  <span className="truncate">{spec.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {spec.provided_by === "publisher" ? "publisher" : "user"}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div>
          <p className="text-xs text-muted-foreground mb-1">Bundle ID</p>
          <code className="font-mono text-xs bg-muted px-1.5 py-0.5 rounded break-all">
            {entry.bundle_id}
          </code>
        </div>

        {entry.latest_revision_number !== null && (
          <div>
            <p className="text-xs text-muted-foreground mb-1">
              Latest revision
            </p>
            <code className="font-mono text-xs bg-muted px-1.5 py-0.5 rounded break-all">
              rev {entry.latest_revision_number}
            </code>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
