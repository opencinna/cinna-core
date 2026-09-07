import type { ManagedAICredentialPublic } from "@/client"
import {
  findAutoProvisionConflict,
  getProviderTypeLabel,
} from "@/components/Admin/LlmProviders/providerTypes"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { USER_ROLE_OPTIONS } from "@/utils/userRoles"

interface CompanyAiCredentialRowProps {
  record: ManagedAICredentialPublic
  /**
   * Every credential the card holds, for the advisory clash hint — a clash is
   * a property of the pair, so a row cannot see one on its own.
   */
  records: ManagedAICredentialPublic[]
  /** This row's write is in flight; its whole toggle group is frozen. */
  isPending: boolean
  onToggle: (role: string, checked: boolean) => void
}

/**
 * One managed AI credential, with the roles that receive it on creation.
 *
 * The three roles are a segmented multi-toggle rather than three columns of a
 * table: guidelines §2 "Toggles on rows" counts the set as **one** control
 * because it is one field of this row's entity (`auto_provision_roles`), which
 * is what lets the card live at half width. Each click is one write, so the
 * group is not a form and holds no draft — `value` is the server's list.
 */
export function CompanyAiCredentialRow({
  record,
  records,
  isPending,
  onToggle,
}: CompanyAiCredentialRowProps) {
  const roles = record.auto_provision_roles ?? []

  return (
    <div className="flex items-center justify-between gap-2 px-3 py-2 border rounded-lg">
      <div className="min-w-0 flex-1">
        <span className="block text-sm font-medium truncate">
          {record.name}
        </span>
        {/* The provider type is the row's one metadata fact, on its own line
            rather than in a `Badge`: at 1024 this row is ~268px, of which the
            toggle group takes ~130, and an "OpenAI Compatible" chip beside the
            name would leave the name under 50px (P3: the kind must not cost
            the name its width). */}
        <p className="text-xs text-muted-foreground truncate mt-0.5">
          {getProviderTypeLabel(record.type)}
        </p>
      </div>

      <ToggleGroup
        type="multiple"
        variant="outline"
        size="sm"
        // Only this row. The stale-read hazard the card's `onSuccess` cache
        // write closes is per-record, so freezing the other rows for one round
        // trip would be a cost with no matching risk. The card tracks the
        // in-flight records itself — `useMutation.isPending` could not answer
        // this, because one observer describes only its latest call.
        disabled={isPending}
        value={roles}
        // The group is fully controlled off `value`, so Radix hands back this
        // exact array plus or minus exactly one entry — a simultaneous grant
        // and revoke is unreachable for `type="multiple"`, and the caller wants
        // the one role that moved, not the whole set.
        //
        // Both halves are `includes` predicates over the server's list rather
        // than a rebuild from `USER_ROLE_OPTIONS`, which is what carries a role
        // this build has not heard of through the write untouched: the row
        // cannot render a segment for it, but it must not silently drop it.
        onValueChange={(next) => {
          const granted = next.find((role) => !roles.includes(role))
          if (granted) {
            onToggle(granted, true)
            return
          }
          const revoked = roles.find((role) => !next.includes(role))
          if (revoked) onToggle(revoked, false)
        }}
        className="shrink-0"
        aria-label={`Roles that receive ${record.name}`}
      >
        {USER_ROLE_OPTIONS.map((role) => {
          const checked = roles.includes(role.value)
          // Advisory only, computed from the list already on screen, so it can
          // be stale: the click still goes to the server and a real refusal is
          // rendered by the card below.
          const clash = checked
            ? null
            : findAutoProvisionConflict(records, record, role.value)
          return (
            <Tooltip key={role.value}>
              <TooltipTrigger asChild>
                <ToggleGroupItem
                  value={role.value}
                  aria-label={`Grant ${record.name} to ${role.label} accounts`}
                  className={cn(
                    "h-7 px-2 text-xs font-medium",
                    // Granted reads as `bg-primary`, the token `Checkbox` and
                    // `Switch` already use for checked. Two reasons it hangs
                    // off `aria-pressed` and not `data-[state=on]`:
                    //
                    // 1. `data-state` does not survive this nesting. Radix
                    //    `Toggle` writes `data-state` *before* spreading the
                    //    props it was handed, and `TooltipTrigger asChild`
                    //    hands down its own `data-state` (open/closed) through
                    //    the `Slot` merge — the child does not declare the key,
                    //    so the tooltip's value wins and lands last on the
                    //    button. Every `data-[state=on]:*` rule here would be
                    //    dead, the primitive's own `bg-accent` included, and a
                    //    granted role would look exactly like an ungranted one.
                    //    `aria-pressed` is untouched by the tooltip.
                    //    Note this cuts both ways: the tooltip is what keeps
                    //    `toggleVariants`' `data-[state=on]:bg-accent` dead.
                    //    Drop the `TooltipTrigger` and that rule revives at the
                    //    same specificity as the `aria-pressed` one below, with
                    //    emitted source order deciding the colour — so remove
                    //    the accent rule from `ui/toggle.tsx` in that change.
                    // 2. `bg-accent` is also the outline variant's hover
                    //    colour, so on a control whose only job is to say which
                    //    roles are granted, hovering an ungranted segment would
                    //    otherwise look like the answer.
                    "aria-pressed:bg-primary aria-pressed:text-primary-foreground",
                    "aria-pressed:hover:bg-primary aria-pressed:hover:text-primary-foreground",
                    clash && "text-amber-600 dark:text-amber-500",
                  )}
                >
                  {role.short}
                </ToggleGroupItem>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                {clash
                  ? `"${clash.name}" already sets this role's default for a mode "${record.name}" also wires. Ticking this will be refused.`
                  : role.label}
              </TooltipContent>
            </Tooltip>
          )
        })}
      </ToggleGroup>
    </div>
  )
}
