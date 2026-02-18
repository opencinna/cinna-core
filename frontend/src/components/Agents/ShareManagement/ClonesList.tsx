import { useMemo } from "react"
import { useMutation } from "@tanstack/react-query"
import { Loader2, Mail, Send, CheckCircle, Clock } from "lucide-react"

import type { AgentPublic, AgentSharePublic } from "@/client"
import { AgentSharesService } from "@/client"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

interface ClonesListProps {
  clones: AgentPublic[]
  shares?: AgentSharePublic[]
  agentId: string
  onUpdatesPushed: () => void
}

export function ClonesList({ clones, shares = [], agentId, onUpdatesPushed }: ClonesListProps) {
  // Build set of clone IDs that were created via email integration
  const emailCloneIds = useMemo(() => {
    const ids = new Set<string>()
    for (const share of shares) {
      if (share.source === "email_integration" && share.cloned_agent_id) {
        ids.add(share.cloned_agent_id)
      }
    }
    return ids
  }, [shares])

  const pushMutation = useMutation({
    mutationFn: () => AgentSharesService.pushUpdatesToClones({ agentId }),
    onSuccess: () => {
      onUpdatesPushed()
    },
  })

  if (clones.length === 0) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        No clones yet. Share this agent to create clones.
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Push updates button */}
      <div className="flex items-center justify-between p-4 bg-muted rounded-lg">
        <div>
          <h4 className="font-medium">Push Updates to All Clones</h4>
          <p className="text-sm text-muted-foreground">
            Send your latest changes to {clones.length} clone(s)
          </p>
        </div>
        <Button
          onClick={() => pushMutation.mutate()}
          disabled={pushMutation.isPending}
        >
          {pushMutation.isPending ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Pushing...
            </>
          ) : (
            <>
              <Send className="h-4 w-4 mr-2" />
              Push Updates
            </>
          )}
        </Button>
      </div>

      {/* Success message */}
      {pushMutation.isSuccess && (
        <Alert className="border-green-200 bg-green-50 dark:border-green-800 dark:bg-green-950">
          <CheckCircle className="h-4 w-4 text-green-600 dark:text-green-400" />
          <AlertDescription className="text-green-800 dark:text-green-200">
            Updates pushed! Automatic clones will update immediately.
            Manual clones will see "Update Available".
          </AlertDescription>
        </Alert>
      )}

      {/* Error */}
      {pushMutation.error && (
        <Alert variant="destructive">
          <AlertDescription>
            {(pushMutation.error as Error).message || "Failed to push updates"}
          </AlertDescription>
        </Alert>
      )}

      {/* Clones table */}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Clone Name</TableHead>
            <TableHead>Mode</TableHead>
            <TableHead>Update Mode</TableHead>
            <TableHead>Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {clones.map((clone) => (
            <TableRow key={clone.id}>
              <TableCell className="font-medium">
                <div className="flex items-center gap-1.5">
                  {clone.name}
                  {emailCloneIds.has(clone.id) && (
                    <Badge variant="outline" className="text-xs px-1.5 py-0 gap-0.5 text-indigo-700 dark:text-indigo-300 border-indigo-300 dark:border-indigo-700">
                      <Mail className="h-3 w-3" />
                      Email
                    </Badge>
                  )}
                </div>
              </TableCell>
              <TableCell>
                <Badge variant="outline">
                  {clone.clone_mode === "builder" ? "Builder" : "User"}
                </Badge>
              </TableCell>
              <TableCell>
                {clone.update_mode === "automatic" ? (
                  <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
                    Automatic
                  </Badge>
                ) : (
                  <Badge variant="secondary">Manual</Badge>
                )}
              </TableCell>
              <TableCell>
                {clone.pending_update ? (
                  <Badge variant="outline" className="text-yellow-700 dark:text-yellow-300">
                    <Clock className="h-3 w-3 mr-1" />
                    Pending
                  </Badge>
                ) : (
                  <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
                    <CheckCircle className="h-3 w-3 mr-1" />
                    Up to date
                  </Badge>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
