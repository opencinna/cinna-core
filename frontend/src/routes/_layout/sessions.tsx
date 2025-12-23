import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"

import { SessionsService, AgentsService } from "@/client"
import { SessionCard } from "@/components/Sessions/SessionCard"
import { CreateSession } from "@/components/Sessions/CreateSession"
import PendingItems from "@/components/Pending/PendingItems"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Label } from "@/components/ui/label"

export const Route = createFileRoute("/_layout/sessions")({
  component: SessionsList,
})

function SessionsList() {
  const [filterMode, setFilterMode] = useState<string>("all")
  const [filterStatus, setFilterStatus] = useState<string>("all")

  const {
    data: sessionsData,
    isLoading: sessionsLoading,
    error: sessionsError,
  } = useQuery({
    queryKey: ["sessions"],
    queryFn: () => SessionsService.listSessions(),
  })

  if (sessionsLoading) {
    return <PendingItems />
  }

  if (sessionsError) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading sessions</p>
      </div>
    )
  }

  const sessions = sessionsData?.data || []

  // Filter sessions
  const filteredSessions = sessions.filter((session) => {
    if (filterMode !== "all" && session.mode !== filterMode) return false
    if (filterStatus !== "all" && session.status !== filterStatus) return false
    return true
  })

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Sessions</h1>
          <p className="text-muted-foreground">
            Manage your conversation sessions with agents
          </p>
        </div>
        <CreateSession />
      </div>

      {/* Filters */}
      <div className="flex gap-4 flex-wrap">
        <div className="flex-1 min-w-[200px]">
          <Label htmlFor="filterMode" className="text-sm mb-2 block">
            Mode
          </Label>
          <Select value={filterMode} onValueChange={setFilterMode}>
            <SelectTrigger id="filterMode">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Modes</SelectItem>
              <SelectItem value="conversation">Conversation</SelectItem>
              <SelectItem value="building">Building</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="flex-1 min-w-[200px]">
          <Label htmlFor="filterStatus" className="text-sm mb-2 block">
            Status
          </Label>
          <Select value={filterStatus} onValueChange={setFilterStatus}>
            <SelectTrigger id="filterStatus">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Statuses</SelectItem>
              <SelectItem value="active">Active</SelectItem>
              <SelectItem value="paused">Paused</SelectItem>
              <SelectItem value="completed">Completed</SelectItem>
              <SelectItem value="error">Error</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Sessions Grid */}
      {filteredSessions.length === 0 ? (
        <div className="text-center py-12 border-2 border-dashed rounded-lg">
          <p className="text-muted-foreground mb-4">
            {sessions.length === 0
              ? "No sessions yet"
              : "No sessions match the selected filters"}
          </p>
          <CreateSession />
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filteredSessions.map((session) => {
            // Get agent name from environment_id (we'll need to improve this with a proper join)
            const agentName = "Agent" // Placeholder - will be improved when we add agent info to session response
            return (
              <SessionCard
                key={session.id}
                session={session}
                agentName={agentName}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
