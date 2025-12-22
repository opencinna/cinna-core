import { Link } from "@tanstack/react-router"
import { Bot } from "lucide-react"

import type { AgentPublic } from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

interface AgentCardProps {
  agent: AgentPublic
}

export function AgentCard({ agent }: AgentCardProps) {
  return (
    <Card className="relative transition-all hover:shadow-md hover:-translate-y-0.5">
      <Link
        to="/agent/$agentId"
        params={{ agentId: agent.id }}
        className="block"
      >
        <CardHeader className="pb-3">
          <div className="flex items-start gap-3 mb-2">
            <div className="rounded-lg bg-primary/10 p-2 text-primary">
              <Bot className="h-5 w-5" />
            </div>
            <div className="flex-1 min-w-0">
              <CardTitle className="text-lg break-words">
                {agent.name}
              </CardTitle>
            </div>
          </div>
          {agent.workflow_prompt && (
            <CardDescription className="line-clamp-2 min-h-[2.5rem]">
              {agent.workflow_prompt}
            </CardDescription>
          )}
        </CardHeader>

        <CardContent>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {agent.entrypoint_prompt && (
              <span className="truncate">Has entrypoint</span>
            )}
          </div>
        </CardContent>
      </Link>
    </Card>
  )
}
