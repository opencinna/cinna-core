import type { CLIAccountTokenPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { CliSessionRow } from "./CliSessionRow"

interface AllCliSessionsSheetProps {
  /** Every account session, in the order the card previews them. */
  tokens: CLIAccountTokenPublic[]
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for Local Development's session list.
 *
 * A Sheet rather than a route, for the same reason `AllAppSessionsSheet` is:
 * the full list needs no search, sort or pagination, and a session has no
 * lifecycle beyond the one action its row already carries.
 */
export function AllCliSessionsSheet({
  tokens,
  open,
  onOpenChange,
}: AllCliSessionsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Account sessions</SheetTitle>
          <SheetDescription>
            {tokens.length} machine{tokens.length === 1 ? "" : "s"} bootstrapped
            from this account
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {tokens.map((token) => (
              <CliSessionRow key={token.id} token={token} />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}
