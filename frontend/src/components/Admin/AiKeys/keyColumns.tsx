import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { Loader2 } from "lucide-react"

import type { AdminAIKeyRow } from "@/client"
import { RowInfo } from "@/components/Common/ListRow"
import { Badge } from "@/components/ui/badge"
import {
  describeProvisionError,
  membershipStatusMeta,
} from "@/utils/keyProvisioning"
import { getProviderTypeLabel } from "../LlmProviders/providerTypes"
import { KeyRowActions } from "./KeyRowActions"
import { SharedKeyRowActions } from "./SharedKeyRowActions"

const TONE_CLASS = {
  neutral: "bg-muted-foreground",
  progress: "bg-primary",
  ok: "bg-success",
  error: "bg-destructive",
} as const

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  })
}

/**
 * Whose key this is.
 *
 * A per-user row leads with the person, because "whose key is this" is the
 * question the list exists to answer; a shared row leads with the credential,
 * because there is no single person to name — its key is held as a copy by
 * everyone on the second line.
 *
 * The vendor handles ride in the `RowInfo` glyph rather than in a column of
 * their own. They are the house's definition of a detail — true of the row, and
 * looked *up* rather than scanned — and a seventh column pushed the table into
 * a horizontal scroll at 1024. Since a minted service account is now named
 * `<email> (<membership id>)` at the provider, the email in the first column is
 * already the match; these are the confirmation.
 */
function KeyCell({ row }: { row: AdminAIKeyRow }) {
  const isShared = row.kind === "shared"
  const title = isShared ? row.credential_name : (row.holder_email ?? "—")
  const attempts = row.provision_attempts ?? 0
  const subtitle = isShared
    ? `Shared with ${row.member_count ?? 0} ${
        row.member_count === 1 ? "person" : "people"
      }`
    : row.credential_name
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <span className="truncate font-medium">{title}</span>
        {row.is_default && (
          <Badge variant="secondary" className="h-5 shrink-0">
            Default
          </Badge>
        )}
        <RowInfo
          facts={[
            row.key_reference && `Provider reference: ${row.key_reference}`,
            // Optional on the wire (it has a server-side default), so the
            // count is read once and defaulted here rather than asserted.
            attempts > 0 &&
              `${attempts} provisioning ${attempts === 1 ? "attempt" : "attempts"}`,
          ]}
        />
      </div>
      <div className="truncate text-xs text-muted-foreground">{subtitle}</div>
    </div>
  )
}

/**
 * Where this key stands.
 *
 * The tone-dot + label form, which §5 of the guidelines admits in a
 * `DataTable` cell: two badges would flatten two unequal facts (the state, and
 * the reason it is in that state) into one shape.
 *
 * A shared row has no provisioning lifecycle — its key was pasted, not minted —
 * so it says so instead of borrowing a status no membership row ever held.
 */
function StatusCell({ row }: { row: AdminAIKeyRow }) {
  if (row.kind === "shared" || !row.provisioning_status) {
    return <span className="text-sm text-muted-foreground">Shared key</span>
  }
  const meta = membershipStatusMeta(row.provisioning_status)
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        {meta.inFlight ? (
          <Loader2 className="size-3 animate-spin text-primary" />
        ) : (
          <span
            className={`size-2 shrink-0 rounded-full ${TONE_CLASS[meta.tone]}`}
          />
        )}
        <span className="truncate text-sm">{meta.adminLabel}</span>
      </div>
      {row.provisioning_status === "failed" && (
        <div className="truncate text-xs text-destructive">
          {describeProvisionError(row.provision_error)}
        </div>
      )}
    </div>
  )
}

/**
 * Where the key comes from: the provider that issued it, or "Manual".
 *
 * Lifted from the deleted `LlmProvidersTable.SourceCell`, including its third
 * state — owned by a provider row that has gone is not "Manual", and calling it
 * that would contradict the menu, which still refuses to delete it.
 */
function SourceCell({ row }: { row: AdminAIKeyRow }) {
  if (row.provider_id) {
    if (!row.provider_name) {
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
        {row.provider_name}
      </Link>
    )
  }
  return (
    <Badge variant="outline" className="text-muted-foreground">
      Manual
    </Badge>
  )
}

export const keyColumns: ColumnDef<AdminAIKeyRow>[] = [
  {
    id: "key",
    header: "Key",
    cell: ({ row }) => <KeyCell row={row.original} />,
  },
  {
    id: "status",
    header: "Status",
    cell: ({ row }) => <StatusCell row={row.original} />,
  },
  {
    id: "type",
    header: "Type",
    cell: ({ row }) => (
      <Badge variant="secondary">
        {getProviderTypeLabel(row.original.type)}
      </Badge>
    ),
  },
  {
    id: "source",
    header: "Source",
    cell: ({ row }) => <SourceCell row={row.original} />,
  },
  {
    id: "created",
    header: "Created",
    cell: ({ row }) => (
      <span className="text-sm text-muted-foreground">
        {formatDate(row.original.created_at)}
      </span>
    ),
  },
  {
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row }) =>
      row.original.kind === "shared" ? (
        <SharedKeyRowActions row={row.original} />
      ) : (
        <KeyRowActions row={row.original} />
      ),
  },
]
