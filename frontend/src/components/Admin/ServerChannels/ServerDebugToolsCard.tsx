import { Link, type LinkProps } from "@tanstack/react-router"
import type { LucideIcon } from "lucide-react"
import { ChevronRight, Compass, Wrench } from "lucide-react"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

/**
 * Server Debug Tools — the shelf the server-side diagnostic instruments live
 * on, so that each new one is an entry in `DEBUG_TOOLS` rather than another
 * card competing for room on the Channels tab.
 *
 * Nothing here does any work itself: every entry is a link to a page that owns
 * its own data, its own loading/error states and its own permissions. That is
 * deliberate — a shelf that fetched would have to decide what a failed fetch
 * means for a link, and the honest answer is "nothing".
 */

interface DebugTool {
  /** Route path — typed against the generated route tree. */
  to: NonNullable<LinkProps["to"]>
  title: string
  /** One line on what question this tool answers. */
  description: string
  icon: LucideIcon
}

/** Add a tool by adding a line here. */
const DEBUG_TOOLS: DebugTool[] = [
  {
    to: "/admin/routing-tuning",
    title: "Auto routing tuning",
    description:
      "Why routing chose the agent it chose — or chose nothing. Read-only.",
    icon: Compass,
  },
]

export function ServerDebugToolsCard() {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Wrench className="h-4 w-4 text-blue-500" />
          Server debug tools
        </CardTitle>
        <CardDescription>
          Instruments for working out what the server actually did. More land
          here as they are built
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-1.5">
        {DEBUG_TOOLS.map((tool) => (
          <Link
            key={tool.title}
            to={tool.to}
            className="flex items-center gap-3 rounded-lg border px-3 py-2 transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <tool.icon className="h-4 w-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{tool.title}</p>
              <p className="text-xs text-muted-foreground">
                {tool.description}
              </p>
            </div>
            <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
          </Link>
        ))}
      </CardContent>
    </Card>
  )
}
