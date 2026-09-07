import type { AICredentialPublic, UserKeyProvisioningPublic } from "@/client"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { KeyProvisioningRows } from "../KeyProvisioningRows"
import { AICredentialRow } from "./AICredentialRow"

interface AllAICredentialsSheetProps {
  /** Every credential, already sorted the way the card sorts them. */
  credentials: AICredentialPublic[]
  /** Keys an administrator is still minting for this user. */
  provisionings: UserKeyProvisioningPublic[]
  /**
   * The N the card's "Show all (N)" printed, so the trigger and the header it
   * opens can never disagree.
   */
  totalCount: number
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the AI Credentials card.
 *
 * A Sheet rather than a route: the full list needs no search, sort or
 * pagination (a user holds single-digit credentials) and an AI credential has
 * no route or lifecycle of its own. The rows are the card's rows — same
 * component, same menu — so the two hosts cannot drift, and the order is the
 * card's order, passed in rather than recomputed.
 */
export function AllAICredentialsSheet({
  credentials,
  provisionings,
  totalCount,
  open,
  onOpenChange,
}: AllAICredentialsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>AI Credentials</SheetTitle>
          <SheetDescription>
            {totalCount} {totalCount === 1 ? "credential" : "credentials"}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto space-y-1.5 px-4 pb-4">
          {credentials.length === 0 && provisionings.length === 0 ? (
            // Reachable without closing the Sheet: the rows carry Delete.
            <p className="text-sm text-muted-foreground">
              You haven&apos;t added an AI credential yet.
            </p>
          ) : (
            <>
              <KeyProvisioningRows rows={provisionings} />
              {credentials.map((credential) => (
                <AICredentialRow key={credential.id} credential={credential} />
              ))}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
