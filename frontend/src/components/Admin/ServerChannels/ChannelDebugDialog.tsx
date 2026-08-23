import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowDownLeft,
  ArrowUpRight,
  RefreshCw,
  Send,
  Trash2,
} from "lucide-react"
import { useState } from "react"

import {
  type ChannelDebugEventPublic,
  type ServerChannelPublic,
  ServerChannelsService,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  channel: ServerChannelPublic
}

/** Poll interval while the dialog is open. Fast enough that a message sent
 *  from the chat app shows up before you switch windows back, slow enough not
 *  to hammer an admin endpoint that reads a lock-guarded buffer. */
const POLL_MS = 3000

type BadgeTone = "outline" | "secondary" | "destructive" | "default"

/** Kind → how it should read at a glance. Unknown kinds fall through to a
 *  neutral badge rather than disappearing, so a newly added event kind on the
 *  backend never renders as a blank row. */
const KIND_META: Record<string, { label: string; tone: BadgeTone }> = {
  received: { label: "Received", tone: "default" },
  rejected: { label: "Rejected", tone: "destructive" },
  routed: { label: "Routed", tone: "secondary" },
  installing: { label: "Installing", tone: "secondary" },
  no_match: { label: "No match", tone: "outline" },
  replied: { label: "Replied", tone: "secondary" },
  send_failed: { label: "Send failed", tone: "destructive" },
  test_send: { label: "Test send", tone: "outline" },
}

function kindMeta(kind: string) {
  return KIND_META[kind] ?? { label: kind, tone: "outline" as BadgeTone }
}

function formatTime(iso: string) {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString()
}

/** `capturing_since` is the backend's process start, and it exists to make "a
 *  restart dropped the buffer" legible. Time-only would read as this morning on
 *  a backend that has been up for days, so this one field carries its date. */
function formatDateTime(iso: string) {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

function DebugEventRow({
  event,
  onSendTest,
  sending,
  canSend,
  busy,
}: {
  event: ChannelDebugEventPublic
  onSendTest: (threadKey: string) => void
  sending: boolean
  canSend: boolean
  busy: boolean
}) {
  const meta = kindMeta(event.kind)
  const inbound = event.direction === "inbound"
  const detail = Object.entries(event.detail ?? {}).filter(([, v]) => v !== "")

  return (
    <div className="space-y-1.5 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        {inbound ? (
          <ArrowDownLeft className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        <Badge variant={meta.tone}>{meta.label}</Badge>
        <span className="font-mono text-xs text-muted-foreground">
          {formatTime(event.at)}
        </span>
        {/* Consecutive identical events collapse into one row; the timestamp
            above is then the latest occurrence. */}
        {(event.repeat ?? 1) > 1 && (
          <Badge variant="outline" title={`Repeated ${event.repeat} times`}>
            ×{event.repeat}
          </Badge>
        )}
        {event.sender_email && (
          <span className="truncate text-xs font-medium">
            {event.sender_display_name
              ? `${event.sender_display_name} <${event.sender_email}>`
              : event.sender_email}
          </span>
        )}
      </div>

      <p className="text-sm">{event.summary}</p>

      {event.text && (
        <p className="rounded bg-muted/50 px-2 py-1.5 text-xs whitespace-pre-wrap break-words">
          {event.text}
        </p>
      )}

      {detail.length > 0 && (
        <p className="font-mono text-[11px] text-muted-foreground break-all">
          {detail.map(([k, v]) => `${k}=${v}`).join("  ")}
        </p>
      )}

      {event.thread_key && (
        <div className="flex items-center justify-between gap-2 pt-0.5">
          <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">
            {event.thread_key}
          </code>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                {/* A disabled button swallows pointer events, so the tooltip
                    that explains WHY it is disabled needs a wrapper to hang
                    off — otherwise the missing-credential case is silent.
                    `tabIndex` keeps that explanation reachable by keyboard. */}
                <span tabIndex={0}>
                  <LoadingButton
                    variant="outline"
                    size="sm"
                    className="h-7 shrink-0 text-xs"
                    loading={sending}
                    // Any in-flight test disables every row: a second `mutate`
                    // supersedes the first observer, so only one toast lands
                    // and it is ambiguous which send it describes.
                    disabled={!canSend || busy}
                    onClick={() => onSendTest(event.thread_key as string)}
                  >
                    <Send className="mr-1.5 h-3 w-3" />
                    Reply here
                  </LoadingButton>
                </span>
              </TooltipTrigger>
              <TooltipContent>
                {!canSend
                  ? "Add the outbound credential to this channel first"
                  : busy
                    ? "A test send is already in flight"
                    : "Send a test message into this exact thread"}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      )}
    </div>
  )
}

/**
 * Live view of what a channel is actually receiving and sending.
 *
 * The backend holds these events in memory only (they are gone after a restart
 * — hence "capturing since"), so this is a debugging aid, not an audit trail:
 * the durable record of denials and verification failures stays in the
 * security event feed.
 */
export function ChannelDebugDialog({ open, onOpenChange, channel }: Props) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  // Keyed on the event id, not the thread key: several events share one thread
  // by construction, and keying on the thread spins every one of those rows.
  const [sendingEventId, setSendingEventId] = useState<string | null>(null)

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["serverChannelDebug", channel.id],
    queryFn: () =>
      ServerChannelsService.listDebugEvents({ channelId: channel.id }),
    enabled: open,
    // Only while the dialog is open — see `enabled`. Without that gate this
    // would keep polling an admin endpoint for the life of the page.
    refetchInterval: open ? POLL_MS : false,
  })

  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: ["serverChannelDebug", channel.id],
    })

  const testMutation = useMutation({
    mutationFn: (threadKey: string) =>
      ServerChannelsService.testOutbound({
        channelId: channel.id,
        requestBody: { thread_key: threadKey },
      }),
    // The route reports failure as a 200 with `success: false` — it is a
    // diagnostic, so the reason travels in the body rather than as an error.
    onSuccess: (result) => {
      if (result.success) showSuccessToast("Test message delivered")
      else showErrorToast(result.error || "Delivery failed")
      invalidate()
    },
    onError: (err) => showErrorToast(getErrorMessage(err, "Test failed")),
    onSettled: () => setSendingEventId(null),
  })

  const clearMutation = useMutation({
    mutationFn: () =>
      ServerChannelsService.clearDebugEvents({ channelId: channel.id }),
    onSuccess: () => {
      invalidate()
      queryClient.invalidateQueries({
        queryKey: ["serverChannelRecentSenders", channel.id],
      })
    },
    onError: (err) => showErrorToast(getErrorMessage(err, "Failed to clear")),
  })

  const events = data?.events ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[720px] max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Debug {channel.name}</DialogTitle>
          <DialogDescription>
            Live view of what this channel receives and sends, and what the
            pipeline decided about each message. Held in memory only — a
            backend restart clears it.
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center justify-between gap-2">
          <p className="text-xs text-muted-foreground">
            {data
              ? `Last ${data.buffer_size} events · capturing since ${formatDateTime(
                  data.capturing_since,
                )}`
              : " "}
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              // Deliberately not disabled on `isFetching`: the 3s poll would
              // grey it out every three seconds. React Query dedupes a
              // concurrent refetch anyway.
              onClick={() => refetch()}
            >
              <RefreshCw
                className={`mr-1.5 h-3 w-3 ${isFetching ? "animate-spin" : ""}`}
              />
              Refresh
            </Button>
            <LoadingButton
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              loading={clearMutation.isPending}
              disabled={events.length === 0}
              onClick={() => clearMutation.mutate()}
            >
              <Trash2 className="mr-1.5 h-3 w-3" />
              Clear
            </LoadingButton>
          </div>
        </div>

        {isError ? (
          <p className="text-sm text-destructive">
            {getErrorMessage(error, "Couldn't load debug events.")}
          </p>
        ) : isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : events.length === 0 ? (
          <div className="rounded-lg border border-dashed p-6 text-center">
            <p className="text-sm text-muted-foreground">
              Nothing captured yet.
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Send the app a message from {channel.channel_type.replace("_", " ")}{" "}
              — it appears here within a few seconds, along with what the
              pipeline decided to do with it.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {events.map((event) => (
              <DebugEventRow
                key={event.id}
                event={event}
                sending={
                  testMutation.isPending && sendingEventId === event.id
                }
                canSend={channel.has_outbound_credentials}
                busy={testMutation.isPending}
                onSendTest={(threadKey) => {
                  setSendingEventId(event.id)
                  testMutation.mutate(threadKey)
                }}
              />
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
