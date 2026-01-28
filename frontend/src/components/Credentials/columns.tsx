import type { ColumnDef } from "@tanstack/react-table"
import { Link } from "@tanstack/react-router"
import { Check, Copy } from "lucide-react"

import type { CredentialPublic } from "@/client"
import { Button } from "@/components/ui/button"
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard"
import { cn } from "@/lib/utils"
import { CredentialActionsMenu } from "./CredentialActionsMenu"

function CopyId({ id }: { id: string }) {
  const [copiedText, copy] = useCopyToClipboard()
  const isCopied = copiedText === id

  return (
    <div className="flex items-center gap-1.5 group">
      <span className="font-mono text-xs text-muted-foreground">{id}</span>
      <Button
        variant="ghost"
        size="icon"
        className="size-6 opacity-0 group-hover:opacity-100 transition-opacity"
        onClick={() => copy(id)}
      >
        {isCopied ? (
          <Check className="size-3 text-green-500" />
        ) : (
          <Copy className="size-3" />
        )}
        <span className="sr-only">Copy ID</span>
      </Button>
    </div>
  )
}

const credentialTypeLabels: Record<string, string> = {
  email_imap: "Email (IMAP)",
  odoo: "Odoo",
  gmail_oauth: "Gmail OAuth",
  gmail_oauth_readonly: "Gmail OAuth (Read-Only)",
  gdrive_oauth: "Google Drive OAuth",
  gdrive_oauth_readonly: "Google Drive OAuth (Read-Only)",
  gcalendar_oauth: "Google Calendar OAuth",
  gcalendar_oauth_readonly: "Google Calendar OAuth (Read-Only)",
  google_service_account: "Google Service Account",
  api_token: "API Token",
}

export const columns: ColumnDef<CredentialPublic>[] = [
  {
    accessorKey: "id",
    header: "ID",
    cell: ({ row }) => <CopyId id={row.original.id} />,
  },
  {
    accessorKey: "name",
    header: "Name",
    cell: ({ row }) => (
      <Link
        to="/credential/$credentialId"
        params={{ credentialId: row.original.id }}
        className="font-medium hover:underline text-primary"
      >
        {row.original.name}
      </Link>
    ),
  },
  {
    accessorKey: "type",
    header: "Type",
    cell: ({ row }) => {
      const type = row.original.type
      return (
        <span className="text-sm">
          {credentialTypeLabels[type] || type}
        </span>
      )
    },
  },
  {
    accessorKey: "notes",
    header: "Notes",
    cell: ({ row }) => {
      const notes = row.original.notes
      return (
        <span
          className={cn(
            "max-w-xs truncate block text-muted-foreground text-sm",
            !notes && "italic",
          )}
        >
          {notes || "No notes"}
        </span>
      )
    },
  },
  {
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <CredentialActionsMenu credential={row.original} />
      </div>
    ),
  },
]
