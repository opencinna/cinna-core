import { useQuery } from "@tanstack/react-query"
import { EllipsisVertical } from "lucide-react"
import { useState } from "react"

import { type AdminAIKeyRow, AdminLlmProvidersService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { LlmProviderActionsMenu } from "../LlmProviders/LlmProviderActionsMenu"
import { MANAGED_CREDENTIALS_QUERY_PREFIX } from "../LlmProviders/providerTypes"

/**
 * The `⋯` for a **shared** key, which is a record's menu.
 *
 * A shared row *is* a managed credential — one pasted key, copied onto every
 * member's child credential — so its verbs are the record's verbs (Members,
 * Set default for all, Edit, Delete) and they are not reimplemented here.
 * `LlmProviderActionsMenu` is rendered whole.
 *
 * That menu needs the full `ManagedAICredentialPublic`, which the keys
 * projection deliberately does not carry: shipping every record's members with
 * every page is the payload this list exists to stop sending. So the record is
 * fetched **on intent** — hover, focus or click — and the real menu replaces
 * this placeholder in the same position the moment it arrives. Hover and focus
 * are what keep it a one-click affordance in practice; the click is the
 * fallback for the pointer that lands without hovering first, and it costs a
 * second click. No row costs a request nobody reached for.
 */
export function SharedKeyRowActions({ row }: { row: AdminAIKeyRow }) {
  const [wanted, setWanted] = useState(false)

  const { data: record } = useQuery({
    queryKey: [
      ...MANAGED_CREDENTIALS_QUERY_PREFIX,
      "record",
      row.managed_credential_id,
    ],
    queryFn: () =>
      AdminLlmProvidersService.getManagedAiCredential({
        managedCredentialId: row.managed_credential_id,
      }),
    enabled: wanted,
    staleTime: 30_000,
  })

  if (record) {
    return <LlmProviderActionsMenu record={record} />
  }
  // The tooltip and the row-naming label are not optional decoration on an
  // icon-only button — they are what `RowActionsMenu` exists to make structural,
  // and this placeholder stands where it will be.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          aria-label={`Actions for the credential ${row.credential_name}`}
          onMouseEnter={() => setWanted(true)}
          onFocus={() => setWanted(true)}
          onClick={() => setWanted(true)}
        >
          <EllipsisVertical className="h-3.5 w-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>Actions</TooltipContent>
    </Tooltip>
  )
}
