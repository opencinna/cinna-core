import type { AdminAICredentialPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { LlmProviderActionsMenu } from "./LlmProviderActionsMenu"
import { getProviderTypeLabel } from "./providerTypes"

interface LlmProvidersTableProps {
  credentials: AdminAICredentialPublic[]
  // Maps owner user id → display label (email or full name). Owners not in the
  // map fall back to a shortened id.
  ownerLabels: Record<string, string>
}

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  })
}

function ownerLabel(ownerId: string, labels: Record<string, string>): string {
  return labels[ownerId] ?? `${ownerId.slice(0, 8)}…`
}

export function LlmProvidersTable({
  credentials,
  ownerLabels,
}: LlmProvidersTableProps) {
  if (credentials.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-md border border-dashed py-20 text-center">
        <p className="text-muted-foreground">No admin-managed credentials found.</p>
        <p className="text-xs text-muted-foreground">
          Provision a credential to get started.
        </p>
      </div>
    )
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Target User</TableHead>
            <TableHead>Name</TableHead>
            <TableHead>Provider</TableHead>
            <TableHead>Default</TableHead>
            <TableHead>Created</TableHead>
            <TableHead className="w-12" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {credentials.map((cred) => (
            <TableRow key={cred.id}>
              <TableCell className="font-medium">
                {ownerLabel(cred.owner_id, ownerLabels)}
              </TableCell>
              <TableCell>{cred.name}</TableCell>
              <TableCell>
                <Badge variant="secondary">{getProviderTypeLabel(cred.type)}</Badge>
              </TableCell>
              <TableCell>
                {cred.is_default ? (
                  <Badge>Default</Badge>
                ) : (
                  <span className="text-muted-foreground text-xs">—</span>
                )}
              </TableCell>
              <TableCell className="text-muted-foreground text-sm">
                {formatDate(cred.created_at)}
              </TableCell>
              <TableCell>
                <LlmProviderActionsMenu credential={cred} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
