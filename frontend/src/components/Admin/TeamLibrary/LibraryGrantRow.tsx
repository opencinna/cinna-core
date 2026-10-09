import { Globe, User, Users, X } from "lucide-react"

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
  LIBRARY_ACCESS_OPTIONS,
  type LibraryAccess,
  type LibraryPrincipal,
  principalKind,
} from "@/utils/libraryAccess"

/** One grant as the access dialog holds it before Save. */
export interface BufferedGrant {
  key: string
  principal: LibraryPrincipal
  access: LibraryAccess
  title: string
}

const KIND_ICON = { everyone: Globe, role: Users, user: User } as const

interface LibraryGrantRowProps {
  grant: BufferedGrant
  disabled: boolean
  onAccessChange: (key: string, access: LibraryAccess) => void
  onRemove: (key: string) => void
}

/**
 * One grant inside the access dialog: who, at what access, and a buffered
 * remove. No status dot — being on the list *is* the access.
 *
 * Maintainer is offered on person rows only: the server lists maintainers
 * from person grants, so a role or Everyone at Maintainer would hold the
 * access without appearing as anyone's maintainer. A non-person row that
 * already carries it (created through the API) still shows its value.
 */
export function LibraryGrantRow({
  grant,
  disabled,
  onAccessChange,
  onRemove,
}: LibraryGrantRowProps) {
  const kind = principalKind(grant.principal)
  const Icon = KIND_ICON[kind]
  const options = LIBRARY_ACCESS_OPTIONS.filter(
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
        onValueChange={(v) => onAccessChange(grant.key, v as LibraryAccess)}
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
