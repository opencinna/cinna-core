import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Play } from "lucide-react"
import { useEffect, useState } from "react"

import { AdminRoutingService } from "@/client"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import {
  RoutingEmpty,
  RoutingLoading,
  RoutingMutationError,
} from "./RoutingStateBlocks"
import { RoutingTraceDetail } from "./RoutingTraceDetail"
import { READ_ONLY_BOUNDARY, SIMULATE_EXPLAINER } from "./routingCopy"

/**
 * "Try a message" — route one message as another user, with no effects.
 *
 * Answers "is my local LLM broken?" in one click: the provider cascade and the
 * raw model output land in the same trace view a stored decision uses, because
 * the backend returns the identical type from both.
 *
 * Two things this deliberately does not do. It does not narrow the user picker
 * to senders who already have a channel binding — the user whose *first*
 * message failed to route has no binding and no trace, and is the main case
 * this tool exists for. And it does not write anything: no binding, no session,
 * no install, no outbound reply.
 *
 * The form is plain `useState` rather than `react-hook-form` + `zod`: three
 * controls, nothing persisted, and the user field is a picker component that is
 * not an RHF-registered input. Validation is "a message and a user", enforced
 * on the submit button.
 */
export function RoutingSimulateDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState("")
  const [selectedUser, setSelectedUser] =
    useState<UserAllowlistSelectedItem | null>(null)
  const [includeCatalog, setIncludeCatalog] = useState(true)

  const mutation = useMutation({
    mutationFn: () =>
      AdminRoutingService.simulateRouting({
        requestBody: {
          message: message.trim(),
          as_user_id: selectedUser?.userId ?? "",
          include_catalog: includeCatalog,
        },
      }),
    onSuccess: () => {
      // A simulate persists a row with `origin="simulate"`, and the card offers
      // an origin filter for exactly those. Without this the run the admin just
      // made is missing from the table until they hit Refresh.
      queryClient.invalidateQueries({ queryKey: ["routingTraces"] })
    },
  })

  // A result describes the inputs that produced it, and nothing on screen says
  // so once those inputs change. That is invisible in precisely the
  // configuration where it matters most — with the message-text gate closed the
  // trace view cannot echo the message back — so the result is dropped the
  // moment an input moves, and again when the dialog closes.
  const { reset } = mutation
  useEffect(() => {
    reset()
  }, [reset, message, selectedUser, includeCatalog])

  useEffect(() => {
    if (!open) reset()
  }, [open, reset])

  const canRun = message.trim().length > 0 && !!selectedUser

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[820px] max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Try a message</DialogTitle>
          <DialogDescription>{SIMULATE_EXPLAINER}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="routing-simulate-message" className="text-xs">
              Message
            </Label>
            <Textarea
              id="routing-simulate-message"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="What would the sender have written?"
              rows={3}
            />
          </div>

          <UserAllowlistPicker
            // Single-select: adding a second user replaces the first.
            selected={selectedUser ? [selectedUser] : []}
            onAdd={(user) =>
              setSelectedUser({
                id: user.id,
                userId: user.id,
                // Without a label the pill renders the raw uuid.
                fallbackLabel: user.full_name || user.email,
              })
            }
            onRemove={() => setSelectedUser(null)}
            // Do not search until the dialog is actually open.
            enabled={open}
            includeSelf
            label={
              <Label className="text-xs">Route as which user?</Label>
            }
            searchPlaceholder="Search users by name or email..."
            emptyHint="Routing sees the agents this user has installed, so the answer depends on who is asking."
          />

          <div className="flex items-center gap-2">
            <Checkbox
              id="routing-simulate-catalog"
              checked={includeCatalog}
              onCheckedChange={(checked) => setIncludeCatalog(checked === true)}
            />
            <Label
              htmlFor="routing-simulate-catalog"
              className="text-xs font-normal text-muted-foreground"
            >
              Include the auto-install catalog (pass 2)
            </Label>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <LoadingButton
              size="sm"
              loading={mutation.isPending}
              disabled={!canRun}
              onClick={() => mutation.mutate()}
            >
              <Play className="mr-1.5 h-3 w-3" />
              Run
            </LoadingButton>
            <span className="text-xs text-muted-foreground">
              {READ_ONLY_BOUNDARY}
            </span>
          </div>

          {mutation.isPending ? (
            <RoutingLoading rows={4} />
          ) : mutation.isError ? (
            <RoutingMutationError
              error={mutation.error}
              fallback="The simulation didn't run."
              onRetry={() => mutation.mutate()}
            />
          ) : mutation.data ? (
            <div className="border-t pt-3">
              <RoutingTraceDetail trace={mutation.data} />
            </div>
          ) : (
            <RoutingEmpty
              title="Nothing run yet."
              hint="Pick a user, type a message, and run it to see exactly what routing would decide."
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
