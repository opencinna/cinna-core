import { useIsMutating } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { AlertTriangle, Copy, Loader2, Package, Trash2 } from "lucide-react"

import { RowFlag } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { AgentApiKeyStatusBadge } from "@/components/Credentials/AgentApiKeyStatusBadge"
import { agentApiKeyStatusSentence } from "@/components/Credentials/agentApiKeyCopy"
import { CredentialRowBadges } from "@/components/Credentials/CredentialRowBadges"
import type { CredentialSection } from "@/components/Credentials/CredentialSectionTabs"
import { getCredentialTypeMeta } from "@/components/Credentials/credentialTypes"
import { deleteCredentialMutationKey } from "@/components/Credentials/DeleteCredential"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard"
import useCustomToast from "@/hooks/useCustomToast"

/**
 * One row of the Credentials table, covering both the caller's own
 * credentials (`CredentialPublic`) and credentials shared with them
 * (`SharedCredentialPublic`). The server-computed `category` decides the
 * section a row is listed under.
 */
export interface CredentialRowModel {
  id: string
  name: string
  type: string
  notes?: string | null
  category: CredentialSection
  agent_usage_count: number
  used_in_bundle: boolean
  is_shared: boolean
  // Owner rows only (`!is_shared`).
  /** People it is shared with directly. */
  share_count?: number
  /** User groups it is shared with. */
  group_share_count?: number
  status?: string | null
  /** Issued agent_api keys only (owner rows): the key's status. */
  agent_api_key_status?: string | null
  // Shared-in rows only (`is_shared`).
  owner_email?: string | null
  shared_at?: string | null
  /** The user groups the share reaches this user through, if any (D12). */
  via_groups?: string[]
}

/** The name as shown; an empty string arrives from the wire, not just null. */
export function credentialDisplayName(credential: CredentialRowModel): string {
  return credential.name || "Unnamed credential"
}

function NameCell({ credential }: { credential: CredentialRowModel }) {
  const { icon: Icon } = getCredentialTypeMeta(credential.type)
  const name = credentialDisplayName(credential)
  const keyStatus = credential.agent_api_key_status

  return (
    <div className="flex min-w-0 items-center gap-2">
      <Icon
        className="h-4 w-4 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      <Tooltip>
        <TooltipTrigger asChild>
          {/* The link is the row's accessible target; the row's own click
              handler only widens the hit area. */}
          <Link
            to="/credential/$credentialId"
            params={{ credentialId: credential.id }}
            className="min-w-0 truncate text-sm font-medium rounded-sm outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            {name}
          </Link>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-sm break-words text-xs">
          {name}
        </TooltipContent>
      </Tooltip>
      {/* The one word a glyph cannot carry: an issued key that no longer
          works. A working key renders nothing. */}
      <AgentApiKeyStatusBadge
        status={keyStatus}
        sentence={keyStatus ? agentApiKeyStatusSentence(keyStatus) : undefined}
        className="shrink-0"
      />
      {/* "Incomplete" is owner-only: a shared credential is never decrypted
          for its recipient. */}
      {!credential.is_shared && credential.status === "incomplete" && (
        <span className="shrink-0">
          <RowFlag
            icon={AlertTriangle}
            tone="warning"
            label="Missing required configuration — open to finish setup"
          />
        </span>
      )}
      {credential.used_in_bundle && (
        <span className="shrink-0">
          <RowFlag icon={Package} label="Used in a bundle" />
        </span>
      )}
    </div>
  )
}

function ActionsCell({
  credential,
  onDelete,
}: {
  credential: CredentialRowModel
  onDelete: (credential: CredentialRowModel) => void
}) {
  const name = credentialDisplayName(credential)
  const deleting =
    useIsMutating({ mutationKey: deleteCredentialMutationKey(credential.id) }) >
    0
  const [, copy] = useCopyToClipboard()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  if (deleting) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className="flex h-7 w-7 items-center justify-center"
            aria-live="polite"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="sr-only">Deleting {name}…</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Deleting…
        </TooltipContent>
      </Tooltip>
    )
  }

  const copyId = async () => {
    if (await copy(credential.id)) {
      showSuccessToast("Credential ID copied")
    } else {
      showErrorToast("Couldn't copy the credential ID")
    }
  }

  return (
    <RowActionsMenu label={`the credential ${name}`}>
      <DropdownMenuItem onSelect={() => void copyId()}>
        <Copy />
        Copy ID
      </DropdownMenuItem>
      {/* A recipient cannot delete what was shared with them; the item is
          absent rather than disabled. */}
      {!credential.is_shared && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            // No preventDefault: the confirm lives at list level, so the menu
            // closes normally instead of staying open under the dialog.
            onSelect={() => onDelete(credential)}
          >
            <Trash2 />
            Delete credential
          </DropdownMenuItem>
        </>
      )}
    </RowActionsMenu>
  )
}

/**
 * Column widths, shared by the table and its loading skeleton so the two never
 * disagree about which columns exist at a width.
 *
 * Name takes what is left. The facts column holds the type badge (≤ 200px)
 * plus, from `lg`, two fixed 48px count slots (sharing, agents); below `lg`
 * it narrows to the type alone.
 */
export const CREDENTIAL_COLUMN_CLASSES = {
  name: "",
  facts: "w-[200px] lg:w-[340px]",
  actions: "w-12 px-2 text-right",
} as const

/**
 * One column set for all three sections: a section is a subset of one entity,
 * and a credential must not read differently because it is listed elsewhere.
 * The table is `table-fixed`, so every text cell truncates instead of wrapping.
 */
export function getCredentialColumns({
  onDelete,
}: {
  onDelete: (credential: CredentialRowModel) => void
}): ColumnDef<CredentialRowModel>[] {
  return [
    {
      id: "name",
      header: "Name",
      cell: ({ row }) => <NameCell credential={row.original} />,
    },
    {
      id: "facts",
      header: () => <span className="sr-only">Type, sharing and agents</span>,
      meta: { className: CREDENTIAL_COLUMN_CLASSES.facts },
      cell: ({ row }) => <CredentialRowBadges credential={row.original} />,
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      meta: { className: CREDENTIAL_COLUMN_CLASSES.actions },
      cell: ({ row }) => (
        <div className="flex justify-end">
          <ActionsCell credential={row.original} onDelete={onDelete} />
        </div>
      ),
    },
  ]
}
