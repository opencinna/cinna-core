import { Loader2, Pencil, ShieldCheck, Trash2 } from "lucide-react"

import type { AgentApiAccessGroupEntry } from "@/client"
import { ListRow, RowFlag } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { pluralize } from "./apiAccess"

interface ApiAccessGroupRowProps {
  group: AgentApiAccessGroupEntry
  /** Per-user scopes are on — otherwise editing them would do nothing. */
  scopesEnabled: boolean
  /** A write on this row is in flight — its name, for the spinner. */
  pendingLabel?: string | null
  onEditScopes: () => void
  onRemove: () => void
}

/**
 * One user group holding scopes on this producer, shared by the card and the
 * "Show all" Sheet. A group has scopes and no keys, so it has no Details of
 * its own and no dot; its scopes use the person row's flag, so the two kinds
 * read the same in one list.
 */
export function ApiAccessGroupRow({
  group,
  scopesEnabled,
  pendingLabel,
  onEditScopes,
  onRemove,
}: ApiAccessGroupRowProps) {
  const scopes = group.scopes ?? []

  return (
    <ListRow
      title={group.name}
      meta={pluralize(group.member_count ?? 0, "member", "members")}
      flags={
        scopes.length > 0 ? (
          <RowFlag icon={ShieldCheck} label={`Scopes: ${scopes.join(", ")}`} />
        ) : undefined
      }
    >
      {pendingLabel ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              className="flex h-7 w-7 items-center justify-center"
              aria-live="polite"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
              <span className="sr-only">{pendingLabel}</span>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            {pendingLabel}
          </TooltipContent>
        </Tooltip>
      ) : (
        <RowActionsMenu label={`${group.name}'s API scopes`}>
          {scopesEnabled && (
            <>
              <DropdownMenuItem onSelect={onEditScopes}>
                <Pencil />
                Edit scopes
              </DropdownMenuItem>
              <DropdownMenuSeparator />
            </>
          )}
          <DropdownMenuItem variant="destructive" onSelect={onRemove}>
            <Trash2 />
            Remove scopes
          </DropdownMenuItem>
        </RowActionsMenu>
      )}
    </ListRow>
  )
}

interface RemoveGroupScopesDialogProps {
  /** The group whose grant is removed; `null` closes the dialog. */
  group: AgentApiAccessGroupEntry | null
  agentName: string
  onOpenChange: (open: boolean) => void
  onConfirm: (group: AgentApiAccessGroupEntry) => void
}

/** Confirm for a group row's Remove scopes (the person row's sibling). */
export function RemoveGroupScopesDialog({
  group,
  agentName,
  onOpenChange,
  onConfirm,
}: RemoveGroupScopesDialogProps) {
  return (
    <AlertDialog open={!!group} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">
            {`Remove ${group?.name || "this group"}'s scopes on ${agentName}?`}
          </AlertDialogTitle>
          <AlertDialogDescription>
            Members lose these scopes from the next call unless granted another
            way.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            onClick={() => {
              if (group) onConfirm(group)
            }}
          >
            Remove scopes
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
