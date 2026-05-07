/**
 * Step 1 — Overview of the bundle being installed.
 */
import { Bot, Globe, Lock, Users } from "lucide-react"
import type { ReactNode } from "react"

import type { CatalogEntryPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

interface WizardStepOverviewProps {
  entry: CatalogEntryPublic
}

const VISIBILITY_ICONS: Record<string, ReactNode> = {
  public: <Globe className="h-3.5 w-3.5" />,
  users: <Users className="h-3.5 w-3.5" />,
  private: <Lock className="h-3.5 w-3.5" />,
}

export function WizardStepOverview({ entry }: WizardStepOverviewProps) {
  const credSpecs = (entry.required_credential_specs ?? []) as Array<{
    name: string
    type: string
  }>

  return (
    <Card>
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
              by {entry.publisher_handle ?? "unknown publisher"}
            </p>
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

        <div>
          <p className="text-xs text-muted-foreground mb-1">Bundle ID</p>
          <code className="font-mono text-xs bg-muted px-1.5 py-0.5 rounded break-all">
            {entry.bundle_id}
          </code>
        </div>

        {credSpecs.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground mb-2">
              This bundle uses the following credentials:
            </p>
            <ul className="space-y-1">
              {credSpecs.map((spec) => (
                <li key={spec.name} className="text-sm flex items-center gap-2">
                  <Badge variant="secondary" className="text-xs">
                    {spec.type}
                  </Badge>
                  <span>{spec.name}</span>
                </li>
              ))}
            </ul>
            <p className="text-xs text-muted-foreground mt-2">
              On the next step you can pick existing credentials to bind, or
              create placeholders to fill in later.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
