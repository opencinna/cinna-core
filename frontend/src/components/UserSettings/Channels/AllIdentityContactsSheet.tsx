import type { IdentityContactPublic } from "@/client"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  IdentityConsentFootnote,
  IdentityContactRow,
} from "./IdentityContactRow"

interface AllIdentityContactsSheetProps {
  /** Everyone, already sorted the way the card sorts them. */
  contacts: IdentityContactPublic[]
  pendingContactIds: ReadonlySet<string>
  open: boolean
  onOpenChange: (open: boolean) => void
  onToggle: (ownerId: string, isEnabled: boolean) => void
}

/**
 * The "Show all (N)" destination for the People card.
 *
 * A Sheet rather than a route: the full list needs no search, sort or
 * pagination, and a person's sharing has no lifecycle this user owns. The
 * consent footnote is repeated here — this is a second reading of the same
 * list, and that sentence is the reason the list is safe to read.
 */
export function AllIdentityContactsSheet({
  contacts,
  pendingContactIds,
  open,
  onOpenChange,
  onToggle,
}: AllIdentityContactsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>People who shared with you</SheetTitle>
          <SheetDescription>
            {contacts.length === 1 ? "1 person" : `${contacts.length} people`}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto space-y-1.5 px-4 pb-4">
          {contacts.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nobody has shared their identity with you yet.
            </p>
          ) : (
            <>
              {contacts.map((contact) => (
                <IdentityContactRow
                  key={contact.owner_id}
                  contact={contact}
                  isPending={pendingContactIds.has(contact.owner_id)}
                  onToggle={(next) => onToggle(contact.owner_id, next)}
                />
              ))}
              <IdentityConsentFootnote />
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
