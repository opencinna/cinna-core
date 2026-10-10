import { User, Users, X } from "lucide-react"

import { ListRow } from "@/components/Common/ListRow"
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
  CATALOG_ACCESS_OPTIONS,
  type CatalogAccess,
  principalKind,
} from "@/utils/catalogAccess"

const KIND_ICON = { role: Users, user: User } as const

interface CatalogGrantRowProps {
  grant: BufferedGrant
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
 * person grants, so a role at Maintainer would hold the access without
 * appearing as anyone's maintainer. A role row that already carries it
 * (created through the API) still shows its value.
 */
export function CatalogGrantRow({
  grant,
  disabled,
  onAccessChange,
  onRemove,
}: CatalogGrantRowProps) {
  const kind = principalKind(grant.principal)
  const Icon = KIND_ICON[kind]
  const options = CATALOG_ACCESS_OPTIONS.filter(
    (o) =>
      o.value !== "maintain" || kind === "user" || grant.access === "maintain",
  )

  return (
    <ListRow
      icon={
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        </span>
      }
      title={grant.title}
    >
      <Select
        value={grant.access}
        onValueChange={(v) => onAccessChange(grant.key, v as CatalogAccess)}
        disabled={disabled}
      >
        <SelectTrigger
          size="sm"
          className="h-8 w-[11.5rem]"
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
