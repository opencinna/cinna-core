import { useState } from "react"
import { Mail, Trash2, User, Wrench } from "lucide-react"

import type { AgentSharePublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { RevokeShareDialog } from "./RevokeShareDialog"

interface ShareListProps {
  shares: AgentSharePublic[]
  agentId: string
  onRevoke: () => void
}

export function ShareList({ shares, agentId, onRevoke }: ShareListProps) {
  const [revokeShare, setRevokeShare] = useState<AgentSharePublic | null>(null)

  if (shares.length === 0) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        No shares yet. Use the "Add Share" tab to share this agent.
      </div>
    )
  }

  const getStatusBadge = (status: string) => {
    switch (status) {
      case "pending":
        return <Badge variant="outline">Pending</Badge>
      case "accepted":
        return <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">Accepted</Badge>
      case "declined":
        return <Badge variant="secondary">Declined</Badge>
      case "revoked":
        return <Badge variant="destructive">Revoked</Badge>
      case "deleted":
        return <Badge className="bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200">Deleted</Badge>
      default:
        return <Badge>{status}</Badge>
    }
  }

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>User</TableHead>
            <TableHead>Access</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Shared</TableHead>
            <TableHead className="w-[100px]">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {shares.map((share) => (
            <TableRow key={share.id}>
              <TableCell className="font-medium">
                <div className="flex items-center gap-1.5">
                  {share.shared_with_email}
                  {share.source === "email_integration" && (
                    <Badge variant="outline" className="text-xs px-1.5 py-0 gap-0.5 text-indigo-700 dark:text-indigo-300 border-indigo-300 dark:border-indigo-700">
                      <Mail className="h-3 w-3" />
                      Email
                    </Badge>
                  )}
                </div>
              </TableCell>
              <TableCell>
                <div className="flex items-center gap-1">
                  {share.share_mode === "builder" ? (
                    <Wrench className="h-4 w-4 text-purple-600 dark:text-purple-400" />
                  ) : (
                    <User className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                  )}
                  {share.share_mode === "builder" ? "Builder" : "User"}
                </div>
              </TableCell>
              <TableCell>{getStatusBadge(share.status)}</TableCell>
              <TableCell className="text-muted-foreground">
                {new Date(share.shared_at).toLocaleDateString()}
              </TableCell>
              <TableCell>
                {share.status !== "revoked" && share.status !== "deleted" && (
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => setRevokeShare(share)}
                  >
                    <Trash2 className="h-4 w-4 text-destructive" />
                  </Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {/* Revoke dialog */}
      {revokeShare && (
        <RevokeShareDialog
          open={!!revokeShare}
          onOpenChange={(open) => !open && setRevokeShare(null)}
          share={revokeShare}
          agentId={agentId}
          onRevoked={() => {
            setRevokeShare(null)
            onRevoke()
          }}
        />
      )}
    </>
  )
}
