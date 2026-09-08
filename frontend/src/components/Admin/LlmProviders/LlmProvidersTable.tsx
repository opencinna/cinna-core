import { Link } from "@tanstack/react-router"

import type { ManagedAICredentialPublic } from "@/client"
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
import { KeyStatusSummary, MemberChip } from "./MemberKeyStatus"
import { getProviderTypeLabel } from "./providerTypes"

interface LlmProvidersTableProps {
  records: ManagedAICredentialPublic[]
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

// Where this credential's key comes from: the provider that owns it, or
// "Manual" for one an admin pasted. Never a blank cell — an empty column reads
// as missing data, and the difference between "manual" and "not loaded" is the
// whole point of the column.
//
// It replaced the auto-provision role chips in the same position and width:
// the rule those chips showed is a property of a provider now, and it is read
// and edited under Providers.
function SourceCell({ record }: { record: ManagedAICredentialPublic }) {
  if (record.is_provider_owned) {
    // Owned but unnamed is a real third state, not "Manual": `_to_public`
    // documents "provider-owned, provider row gone" and keys `has_api_key` off
    // exactly that distinction. Calling it manual here would contradict the row
    // menu, which hides Delete on `is_provider_owned` alone.
    if (!record.provider_name) {
      return (
        <Badge variant="outline" className="text-muted-foreground">
          Provider (missing)
        </Badge>
      )
    }
    return (
      <Link
        to="/admin/ai-credentials"
        hash="providers"
        className="text-primary hover:underline"
      >
        {record.provider_name}
      </Link>
    )
  }
  return (
    <Badge variant="outline" className="text-muted-foreground">
      Manual
    </Badge>
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
            <TableHead className="w-[11%]">Type</TableHead>
            <TableHead className="w-[11%]">Default key</TableHead>
            <TableHead className="w-[9%]">Default SDK</TableHead>
            <TableHead className="w-[12%]">Source</TableHead>
            <TableHead className="w-[12%]">Keys</TableHead>
            <TableHead>Members</TableHead>
            <TableHead className="w-[10%]">Created</TableHead>
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
                <SourceCell record={record} />
              </TableCell>
              <TableCell>
                <KeyStatusSummary record={record} />
              </TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-2">
                  {(record.members ?? []).map((member) => (
                    <MemberChip
                      key={member.user_id}
                      record={record}
                      member={member}
                    />
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
