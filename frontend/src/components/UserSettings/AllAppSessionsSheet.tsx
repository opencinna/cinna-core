import type { DesktopOAuthClientPublic } from "@/client"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { AppSessionRow } from "./AppSessionRow"

interface AllAppSessionsSheetProps {
  /** Every connected app, already sorted the way the card sorts them. */
  clients: DesktopOAuthClientPublic[]
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the App Sessions card.
 *
 * A Sheet rather than a route, by P5's test: the full list needs no search (a
 * user recognises their own devices), no sort ("most recently used" is the only
 * ordering a security list wants, and it is already applied), no pagination
 * (the endpoint takes no page params and returns everything) and the entity has
 * no lifecycle of its own — an app session is created by the app and its only
 * verb is the revoke already on the row.
 *
 * The rows are the card's rows — same component, same confirm — so the two
 * hosts cannot drift. N is the array length, which is the true total precisely
 * because the endpoint is unpaginated.
 */
export function AllAppSessionsSheet({
  clients,
  open,
  onOpenChange,
}: AllAppSessionsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>App Sessions</SheetTitle>
          <SheetDescription>
            {clients.length === 1
              ? "1 connected app"
              : `${clients.length} connected apps`}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto space-y-1.5 px-4 pb-4">
          {clients.length === 0 ? (
            // Reachable without closing the Sheet: the rows carry Disconnect.
            <p className="text-sm text-muted-foreground">
              No apps are connected to this account.
            </p>
          ) : (
            clients.map((client) => (
              <AppSessionRow key={client.client_id} client={client} />
            ))
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
