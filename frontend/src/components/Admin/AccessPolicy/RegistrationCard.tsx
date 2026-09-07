import { Pencil, ShieldCheck } from "lucide-react"
import { useState } from "react"

import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { AllowedEmailPatternsDialog } from "./AllowedEmailPatternsDialog"
import {
  describePatternCount,
  describeRegistration,
  parsePatterns,
  pendingConfigField,
  REGISTRATION_MODE_INVITE_ONLY,
  REGISTRATION_MODE_OPEN,
  useServerConfig,
  useServerConfigUpdate,
} from "./accessPolicy"

/** "*@acme.com · *@corp.example" and "+3 more" — a glance, not the list. */
function previewPatterns(patterns: string[]): string {
  const head = patterns.slice(0, 2).join(" · ")
  const rest = patterns.length - 2
  return rest > 0 ? `${head} +${rest} more` : head
}

/**
 * Who is allowed to create their own account here, and from which addresses.
 *
 * One concern, two blocks, one interaction model: the mode select saves on
 * change, and the pattern list — the only part of this concern with a form —
 * is a summary row that opens its own dialog. That split is what keeps a
 * Save/Cancel form out of an auto-saving card.
 */
export function RegistrationCard() {
  const [patternsOpen, setPatternsOpen] = useState(false)
  const { data: config, isError, error, refetch } = useServerConfig()
  const updateMutation = useServerConfigUpdate()

  const registrationMode = config?.registration_mode ?? REGISTRATION_MODE_OPEN
  const inviteOnly = registrationMode !== REGISTRATION_MODE_OPEN
  const persistedPatterns = config?.allowed_email_patterns ?? ""
  const patterns = parsePatterns(persistedPatterns)

  const pendingField = pendingConfigField(updateMutation)

  const body = () => {
    // Error before "no data": a failed read that renders the `??` defaults
    // above would tell an administrator this instance is open to anyone, which
    // is the one wrong answer this card can give.
    //
    // Gated on there being no data, not on `isError` alone: a *background*
    // refetch that fails reports `status: "error"` while the last good value
    // is still in the cache, and replacing a correct, live card with an error
    // panel because a focus-refetch blipped is its own kind of lie.
    if (isError && config === undefined) {
      return (
        <QueryErrorAlert
          error={error}
          fallback="Couldn't read the access policy."
          onRetry={() => refetch()}
        />
      )
    }

    // Anything else without data is still on its way (loading, or paused
    // offline) — never the `??` defaults.
    if (!config) {
      return (
        <div className="space-y-4">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      )
    }

    return (
      <div className="space-y-4">
        {/* Stacked until the card is wide enough to hold a 240px control
            beside its explanation: between `lg` and `xl` this card is half of
            a ~1000px page, and a shrink-0 trigger next to it wraps the
            description one word per line. */}
        <div className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between xl:gap-4">
          <div className="min-w-0">
            <Label htmlFor="registration-mode" className="text-sm font-medium">
              Registration
            </Label>
            <p className="text-xs text-muted-foreground">
              {inviteOnly
                ? "Only people you invite get an account. Signing in with Google never creates one."
                : "Anyone whose email matches the allowed addresses can create an account."}
            </p>
          </div>
          <Select
            value={registrationMode}
            onValueChange={(value) =>
              updateMutation.mutate({ registration_mode: value })
            }
            disabled={pendingField === "registration_mode"}
          >
            <SelectTrigger
              id="registration-mode"
              className="w-full xl:w-[240px] xl:shrink-0"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={REGISTRATION_MODE_OPEN}>
                Anyone with an allowed email
              </SelectItem>
              <SelectItem value={REGISTRATION_MODE_INVITE_ONLY}>
                Invite only
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
          <div className="min-w-0">
            <p className="text-xs text-muted-foreground">
              Allowed email addresses
            </p>
            <p className="text-sm font-medium">
              {describePatternCount(patterns.length)}
            </p>
            {patterns.length > 0 && (
              <p className="truncate font-mono text-xs text-muted-foreground">
                {previewPatterns(patterns)}
              </p>
            )}
          </div>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => setPatternsOpen(true)}
                aria-label="Edit allowed addresses"
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Edit allowed addresses</TooltipContent>
          </Tooltip>
        </div>
      </div>
    )
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <ShieldCheck className="h-5 w-5" />
          Registration
        </CardTitle>
        <CardDescription>
          {/* Derived from the persisted row only. A sentence that narrated an
              unsaved draft would describe a front door that is not open. */}
          {config
            ? describeRegistration({
                registrationOpen: !inviteOnly,
                patternCount: patterns.length,
              })
            : "Who can create an account here."}
        </CardDescription>
      </CardHeader>
      <CardContent>{body()}</CardContent>
      <AllowedEmailPatternsDialog
        open={patternsOpen}
        onOpenChange={setPatternsOpen}
        persisted={persistedPatterns}
      />
    </Card>
  )
}
