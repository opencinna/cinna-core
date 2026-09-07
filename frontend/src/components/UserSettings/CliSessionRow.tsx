import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Laptop, Unplug } from "lucide-react"
import { useState } from "react"

import type { CLIAccountTokenPublic } from "@/client"
import { CliService } from "@/client"
import { ListRow } from "@/components/Common/ListRow"
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
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

/** The row's one metadata line: what disconnecting this machine would cost. */
function metadataLine(token: CLIAccountTokenPublic): string {
  const facts = [
    `${token.child_count} agent${token.child_count === 1 ? "" : "s"} synced`,
  ]
  if (token.desktop_session_count > 0) {
    facts.push(
      `${token.desktop_session_count} desktop session${
        token.desktop_session_count === 1 ? "" : "s"
      }`,
    )
  }
  return facts.join(" · ")
}

interface CliSessionRowProps {
  token: CLIAccountTokenPublic
}

/**
 * One machine that bootstrapped a local account workspace, as a house list row.
 *
 * Deliberately the same shape as `AppSessionRow`: both are View lists of
 * connected machines whose only mutation is "cut this one off", so both keep
 * the metadata line (what you would lose) and hover-reveal the one destructive
 * control rather than burying it in a `⋯` menu of a single item.
 *
 * The confirm is owned by the row, not the host — that is what lets the "Show
 * all" Sheet stay open behind it and keeps card and Sheet on one code path.
 */
export function CliSessionRow({ token }: CliSessionRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmOpen, setConfirmOpen] = useState(false)

  const label = token.name || token.prefix

  const revokeMutation = useMutation({
    mutationFn: () => CliService.revokeAccountToken({ tokenId: token.id }),
    onSuccess: () => {
      showSuccessToast("Account session disconnected")
      setConfirmOpen(false)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to disconnect")),
    // The likeliest failure is a row that is already gone — revoked from
    // another tab or by the machine itself — which refetching clears.
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["account-cli-tokens"] })
    },
  })

  return (
    <ListRow
      icon={
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
          <Laptop className="h-3.5 w-3.5 text-muted-foreground" />
        </span>
      }
      title={label}
      meta={metadataLine(token)}
    >
      {/* Hover-revealed (P3 variant): a View surface, and a destructive control
          visible on every row invites the accident. */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
            disabled={revokeMutation.isPending}
            aria-label={`Disconnect ${label}`}
            onClick={() => setConfirmOpen(true)}
          >
            <Unplug className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Disconnect
        </TooltipContent>
      </Tooltip>

      <AlertDialog
        open={confirmOpen}
        // Escape would otherwise close the confirm mid-request, leaving the
        // pending state with nowhere to live.
        onOpenChange={(next) => {
          if (!revokeMutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent onOpenAutoFocus={(e) => e.preventDefault()}>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect account session</AlertDialogTitle>
            <AlertDialogDescription>
              Disconnect <strong>{label}</strong>? This cuts off the{" "}
              {token.child_count} agent
              {token.child_count === 1 ? "" : "s"} synced from that machine
              {token.desktop_session_count > 0 ? (
                <>
                  {" "}
                  <strong>
                    and signs out {token.desktop_session_count} Cinna Desktop
                    session
                    {token.desktop_session_count === 1 ? "" : "s"}
                  </strong>{" "}
                  linked from its account file
                </>
              ) : null}
              . Local files stay intact, but the CLI has to be set up again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revokeMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              autoFocus
              onClick={(e) => {
                // Keep the confirm open while the request is in flight so the
                // pending state has somewhere to live.
                e.preventDefault()
                revokeMutation.mutate()
              }}
              disabled={revokeMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {revokeMutation.isPending ? "Disconnecting…" : "Disconnect"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ListRow>
  )
}
