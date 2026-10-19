import { X } from "lucide-react"

import { ListRow, RowInfo } from "@/components/Common/ListRow"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  type BufferedGrant,
  type CatalogAccess,
  catalogAccessOptions,
  principalKind,
} from "@/utils/catalogAccess"
import type { CatalogKind } from "@/utils/catalogItems"
import { pluralize } from "@/utils/pluralize"
import { PRINCIPAL_KIND_ICON } from "@/utils/principalKinds"

interface CatalogGrantRowProps {
  grant: BufferedGrant
  /** The item's kind, which decides what the `edit` level is called. */
  kind: CatalogKind
  disabled: boolean
  onAccessChange: (key: string, access: CatalogAccess) => void
  onRemove: (key: string) => void
}

/**
 * One grant inside the People dialog: who, at what access, and a buffered
 * remove (2 inline controls, R5). No status dot — being on the list *is* the
 * access.
 *
 * Maintainer is offered on person rows only: the server lists maintainers from
 * person grants, so a role or group at Maintainer would hold the access
 * without appearing as anyone's maintainer (D6; the server refuses it for a
 * group). A role row that already carries it (created through the API) still
 * shows its value. A group's member count is its detail flag, not a meta
 * line, so the list keeps one row height across kinds.
 *
 * The `edit` level's words follow the item's kind (`catalogAccessOptions`);
 * a Cinna-hosted bundle offers it only to a row that already holds it.
 */
export function CatalogGrantRow({
  grant,
  kind: itemKind,
  disabled,
  onAccessChange,
  onRemove,
}: CatalogGrantRowProps) {
  const kind = principalKind(grant.principal)
  const Icon = PRINCIPAL_KIND_ICON[kind]
  // The saved level, not the buffered one: a legacy `edit` row changed to
  // Can use must still be able to go back before Save.
  const options = catalogAccessOptions(itemKind, grant.savedAccess).filter(
    (o) =>
      o.value !== "maintain" ||
      kind === "user" ||
      grant.savedAccess === "maintain" ||
      grant.access === "maintain",
  )

  return (
    <ListRow
      icon={
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        </span>
      }
      title={grant.title}
      flags={
        kind === "group" && grant.memberCount != null ? (
          <RowInfo
            facts={[
              `Group · ${pluralize(grant.memberCount, "member", "members")}`,
            ]}
          />
        ) : undefined
      }
    >
      <Select
        value={grant.access}
        onValueChange={(v) => onAccessChange(grant.key, v as CatalogAccess)}
        disabled={disabled}
      >
        <SelectTrigger
          size="sm"
          className="h-8 w-[13rem]"
          aria-label={`Access for ${grant.title}`}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-7 w-7 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
            aria-label={`Remove ${grant.title}`}
            data-remove-key={grant.key}
            disabled={disabled}
            onClick={() => onRemove(grant.key)}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Remove
        </TooltipContent>
      </Tooltip>
    </ListRow>
  )
}
