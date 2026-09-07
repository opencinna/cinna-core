import { BookOpen, EllipsisVertical, Key, Plus } from "lucide-react"
import { useMemo, useState } from "react"

import type { AICredentialPublic } from "@/client"
import { PREVIEW_COUNT, PreviewList } from "@/components/Common/PreviewList"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { AnthropicCredentialsModal } from "../AnthropicCredentialsModal"
import {
  KeyProvisioningRows,
  useMyKeyProvisionings,
} from "../KeyProvisioningRows"
import { AddAICredentialWizard } from "./AddAICredentialWizard"
import { AICredentialRow } from "./AICredentialRow"
import { AllAICredentialsSheet } from "./AllAICredentialsSheet"
import { isExpiryUrgent } from "./credentialTypes"

/** In-flight mints shown above the credentials. A user realistically holds 0–2. */
const PROVISIONING_PREVIEW_COUNT = 2

interface AICredentialsCardProps {
  credentials: AICredentialPublic[]
  /** `count` from the list endpoint — the list's true total. */
  totalCount: number
  isLoading: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
}

/**
 * "AI Credentials" — which keys the user holds, which one is the default, and
 * whether anything is about to expire.
 *
 * A View surface (guidelines §1): a glance, not a control panel. Every
 * mutation is one level down — the row's `⋯` menu, the Add wizard, the edit
 * dialog, the delete confirm — so nothing on the card changes its height and
 * the card holds one interaction model.
 *
 * The two lists it shows are two different things and keep two caps: every row
 * in the credentials list is a usable key, and a provisioning row is a
 * membership with no key behind it yet.
 */
export function AICredentialsCard({
  credentials,
  totalCount,
  isLoading,
  isError,
  error,
  onRetry,
}: AICredentialsCardProps) {
  const [isAddOpen, setIsAddOpen] = useState(false)
  const [isSheetOpen, setIsSheetOpen] = useState(false)
  const [isGuideOpen, setIsGuideOpen] = useState(false)

  const { data: provisioningsData } = useMyKeyProvisionings()
  const provisionings = useMemo(
    () => provisioningsData ?? [],
    [provisioningsData],
  )

  // Most relevant first: the default key, then anything expired or expiring
  // within a month, then whatever changed most recently.
  const sorted = useMemo(() => {
    const rank = (c: AICredentialPublic) =>
      c.is_default ? 0 : isExpiryUrgent(c.expiry_notification_date) ? 1 : 2
    return [...credentials].sort((a, b) => {
      const byRank = rank(a) - rank(b)
      if (byRank !== 0) return byRank
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    })
  }, [credentials])

  // The number on "Show all (N)" is the number of rows the Sheet will hold, so
  // it counts both lists.
  const combinedTotal = totalCount + provisionings.length

  // A row is only ever hidden when something on the card reveals it. The
  // provisioning cap therefore applies exactly when the two lists together
  // exceed the card's row budget — which is when a "Show all" link appears.
  const isProvisioningCapped = combinedTotal > PREVIEW_COUNT
  const shownProvisionings = isProvisioningCapped
    ? provisionings.slice(0, PROVISIONING_PREVIEW_COUNT)
    : provisionings
  // With no credentials at all, `PreviewList` renders its empty branch and no
  // link, so the provisioning list carries its own way to the Sheet rather
  // than hiding rows behind nothing.
  const needsOwnShowAll = isProvisioningCapped && sorted.length === 0

  return (
    <Card>
      <CardHeader>
        {/* Title and actions share one row; the description spans the full
            header width below them, so it stays one line at 1024 instead of
            being squeezed into a column beside the buttons. */}
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <Key className="h-5 w-5 shrink-0" />
            AI Credentials
          </CardTitle>
          <div className="flex items-center gap-1 shrink-0">
            {/* "Add" rather than "Add credential": at 1024 the full label
                plus the ⋯ pushed the card title onto two lines, and the card
                the button sits in is named "AI Credentials". */}
            <Button size="sm" onClick={() => setIsAddOpen(true)}>
              <Plus className="h-4 w-4 mr-2" />
              Add
            </Button>
            <DropdownMenu>
              <Tooltip>
                <TooltipTrigger asChild>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      aria-label="More actions"
                    >
                      <EllipsisVertical className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                </TooltipTrigger>
                <TooltipContent side="top" className="text-xs">
                  More actions
                </TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onSelect={(e) => {
                    e.preventDefault()
                    setIsGuideOpen(true)
                  }}
                >
                  <BookOpen />
                  Anthropic setup guide
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        <CardDescription>
          Named API keys your agents and environments run on.
        </CardDescription>
      </CardHeader>

      <CardContent>
        <div className="space-y-1.5">
          <KeyProvisioningRows rows={shownProvisionings} />
          {/* The four states, the cap and the "Show all" link are the shared P5
              primitive's job; the row and the copy stay here. */}
          <PreviewList
            items={sorted}
            total={combinedTotal}
            getKey={(credential) => credential.id}
            renderItem={(credential) => (
              <AICredentialRow credential={credential} />
            )}
            isLoading={isLoading}
            isError={isError}
            error={error}
            onRetry={onRetry}
            errorFallback="Couldn't load your AI credentials"
            empty={
              // "You haven't added one yet" is false in front of somebody whose
              // key is being minted right now, so the empty state is gated on
              // both lists. The header's Add button is its action.
              provisionings.length > 0 ? null : (
                <p className="text-sm text-muted-foreground">
                  You haven&apos;t added an AI credential yet.
                </p>
              )
            }
            onShowAll={() => setIsSheetOpen(true)}
          />
          {needsOwnShowAll && (
            <Button
              variant="link"
              size="sm"
              className="px-0"
              onClick={() => setIsSheetOpen(true)}
            >
              Show all ({combinedTotal})
            </Button>
          )}
        </div>
      </CardContent>

      {isAddOpen && <AddAICredentialWizard open onOpenChange={setIsAddOpen} />}

      <AllAICredentialsSheet
        credentials={sorted}
        provisionings={provisionings}
        totalCount={combinedTotal}
        open={isSheetOpen}
        onOpenChange={setIsSheetOpen}
      />

      {/* Long-form reference reading, one disclosure level from the card —
          never opened from inside the Add wizard, which is what made it a
          dialog on top of a dialog. */}
      <AnthropicCredentialsModal
        open={isGuideOpen}
        onOpenChange={setIsGuideOpen}
      />
    </Card>
  )
}
