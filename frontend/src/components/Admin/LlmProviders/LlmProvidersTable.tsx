import type { ManagedAICredentialMember, ManagedAICredentialPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { userRoleLabel } from "@/utils/userRoles"
import { LlmProviderActionsMenu } from "./LlmProviderActionsMenu"
import { getProviderTypeLabel } from "./providerTypes"

interface LlmProvidersTableProps {
  records: ManagedAICredentialPublic[]
}

function memberBadgeLabel(member: ManagedAICredentialMember): string {
  if (member.full_name) return `${member.full_name} <${member.email}>`
  return member.email
}

function BooleanBadge({ value }: { value: boolean }) {
  if (value) {
    return <Badge variant="secondary">Yes</Badge>
  }
  return (
    <Badge variant="outline" className="text-muted-foreground">
      No
    </Badge>
  )
}

// Roles whose new accounts receive this credential automatically. "Off" rather
// than a blank cell: an empty column reads as missing data, and the difference
// between "no roles" and "not loaded" is the whole point of the column.
function AutoProvisionCell({ roles }: { roles: string[] }) {
  if (roles.length === 0) {
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Off
      </Badge>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {roles.map((role) => (
        <Badge key={role} variant="secondary" className="whitespace-nowrap">
          {userRoleLabel(role)}
        </Badge>
      ))}
    </div>
  )
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

export function LlmProvidersTable({ records }: LlmProvidersTableProps) {
  if (records.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-md border border-dashed py-20 text-center">
        <p className="text-muted-foreground">No managed credentials found.</p>
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
            <TableHead className="w-[16%]">Name</TableHead>
            <TableHead className="w-[11%]">Provider</TableHead>
            <TableHead className="w-[11%]">Default provider</TableHead>
            <TableHead className="w-[9%]">Default SDK</TableHead>
            <TableHead className="w-[14%]">Auto</TableHead>
            <TableHead>Shared with</TableHead>
            <TableHead className="w-[11%]">Created</TableHead>
            <TableHead className="w-[48px]" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {records.map((record) => (
            <TableRow key={record.id} className="align-top">
              <TableCell className="font-medium">{record.name}</TableCell>
              <TableCell>
                <Badge variant="secondary">{getProviderTypeLabel(record.type)}</Badge>
              </TableCell>
              <TableCell>
                <BooleanBadge value={Boolean(record.set_as_default)} />
              </TableCell>
              <TableCell>
                <BooleanBadge value={Boolean(record.set_user_sdk_defaults)} />
              </TableCell>
              <TableCell>
                <AutoProvisionCell roles={record.auto_provision_roles ?? []} />
              </TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-2">
                  {(record.members ?? []).map((member) => (
                    <div
                      key={member.user_id}
                      className="inline-flex items-center gap-1.5 rounded-full border bg-muted/40 px-3 py-0.5 text-xs"
                    >
                      <span className="truncate">{memberBadgeLabel(member)}</span>
                    </div>
                  ))}
                  {(record.members ?? []).length === 0 && (
                    <span className="text-xs text-muted-foreground">No members</span>
                  )}
                </div>
              </TableCell>
              <TableCell className="text-muted-foreground text-sm">
                {formatDate(record.created_at)}
              </TableCell>
              <TableCell className="text-right">
                <LlmProviderActionsMenu record={record} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
