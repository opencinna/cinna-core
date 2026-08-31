import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ChevronDown,
  ChevronRight,
  EyeOff,
  Compass,
  Play,
  RefreshCw,
  Trash2,
} from "lucide-react"
import { useEffect, useState } from "react"

import {
  AdminRoutingService,
  type RoutingDecisionSummary,
  ServerChannelsService,
} from "@/client"
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
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { RoutingSimulateDialog } from "./RoutingSimulateDialog"
import {
  RoutingEmpty,
  RoutingError,
  RoutingLoading,
  RoutingNotice,
} from "./RoutingStateBlocks"
import { RoutingTraceDetailLoader } from "./RoutingTraceDetail"
import {
  CARD_DESCRIPTION,
  formatConfidence,
  formatDateTime,
  formatLatency,
  MESSAGE_ABSENT,
  MESSAGE_WITHHELD_NO_NOTICE,
  messageTextState,
  ORIGIN_FILTER_OPTIONS,
  originMeta,
  OUTCOME_FILTER_OPTIONS,
  outcomeMeta,
} from "./routingCopy"
import { installRoutingRateLimitCapture } from "./routingRateLimit"

/**
 * Auto Routing Tuning — the whole of `/admin/routing-tuning`, reached from the
 * Server Debug Tools card on `/admin/server-configuration#channels`.
 *
 * The instrument for "why didn't the bot find my agent?". It is **read-only
 * with respect to agents** (plan §1): nothing on this card, or any of its
 * children, edits another user's agent, trigger prompt or bundle. The advisory
 * end of it produces copyable wording for the agent's owner and stops there.
 *
 * **Four states everywhere, not three.** A missing `isError` branch on this
 * card in particular does not merely look untidy — it makes the instrument lie
 * about the system it measures. An admin who opens it to find out why routing
 * failed, and sees "no routing decisions yet" because the request 500'd,
 * concludes routing never ran. That is the same defect the whole feature exists
 * to remove (§11a Rule 1: the dangerous state must not be able to look
 * routine), arriving one layer up. So loading, error, empty and data are
 * distinct in every panel, and the server's own `notice` — which explains an
 * empty page that is empty *on purpose* — is a fifth thing again, never folded
 * into the empty copy.
 */

const PAGE_SIZE = 25

/** shadcn `SelectItem` cannot hold an empty value, so "no filter" needs a name. */
const ANY = "__any__"

export function AutoRoutingTuningCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [channelId, setChannelId] = useState<string>(ANY)
  const [outcome, setOutcome] = useState<string>(ANY)
  const [origin, setOrigin] = useState<string>(ANY)
  const [page, setPage] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [simulateOpen, setSimulateOpen] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)

  // The 429 countdown reads `Retry-After`, which the generated client does not
  // expose on `ApiError`; this hooks the response interceptor that captures it.
  useEffect(() => installRoutingRateLimitCapture(), [])

  const filters = {
    channelId: channelId === ANY ? null : channelId,
    outcome: outcome === ANY ? null : outcome,
    origin: origin === ANY ? null : origin,
  }

  const tracesQuery = useQuery({
    queryKey: ["routingTraces", filters, page],
    queryFn: () =>
      AdminRoutingService.listRoutingTraces({
        channelId: filters.channelId,
        outcome: filters.outcome,
        origin: filters.origin,
        skip: page * PAGE_SIZE,
        limit: PAGE_SIZE,
      }),
  })

  // Shares the `serverChannels` key with the channels card on the server
  // configuration page — a cache hit when the admin came from there, a cold
  // fetch otherwise, which is why the trigger below has all three states.
  const channelsQuery = useQuery({
    queryKey: ["serverChannels"],
    queryFn: () => ServerChannelsService.listChannels(),
  })

  const clearMutation = useMutation({
    mutationFn: () =>
      AdminRoutingService.clearRoutingTraces(
        // The unscoped form has to be asked for by name on the backend, and it
        // is asked for by name here too — never reached by omitting a filter.
        filters.channelId ? { channelId: filters.channelId } : { all: true },
      ),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["routingTraces"] })
      queryClient.invalidateQueries({ queryKey: ["routingTrace"] })
      setExpanded(null)
      setPage(0)
      showSuccessToast(result.message)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to clear routing traces")),
    onSettled: () => setConfirmClear(false),
  })

  const rows = tracesQuery.data?.data ?? []
  const total = tracesQuery.data?.count ?? 0
  const notice = tracesQuery.data?.notice ?? null
  const channels = channelsQuery.data ?? []

  const resetPage = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value)
    setPage(0)
    setExpanded(null)
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Compass className="h-4 w-4 text-blue-500" />
          Auto routing tuning
        </CardTitle>
        <CardDescription>{CARD_DESCRIPTION}</CardDescription>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={channelId} onValueChange={resetPage(setChannelId)}>
            <SelectTrigger className="h-8 w-[13rem] text-xs">
              <SelectValue
                placeholder={
                  // Three placeholders, because a failed fetch and an
                  // in-flight one must not both read as "there are no
                  // channels" — the admin would filter against a list that is
                  // missing, not absent.
                  channelsQuery.isError
                    ? "Couldn't load channels"
                    : channelsQuery.isLoading
                      ? "Loading channels..."
                      : "All channels"
                }
              />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>All channels</SelectItem>
              {channels.map((channel) => (
                <SelectItem key={channel.id} value={channel.id}>
                  {channel.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={outcome} onValueChange={resetPage(setOutcome)}>
            <SelectTrigger className="h-8 w-[10rem] text-xs">
              <SelectValue placeholder="Any outcome" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>Any outcome</SelectItem>
              {OUTCOME_FILTER_OPTIONS.map((value) => (
                <SelectItem key={value} value={value}>
                  {outcomeMeta(value).label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={origin} onValueChange={resetPage(setOrigin)}>
            <SelectTrigger className="h-8 w-[10rem] text-xs">
              <SelectValue placeholder="Any origin" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>Any origin</SelectItem>
              {ORIGIN_FILTER_OPTIONS.map((value) => (
                <SelectItem key={value} value={value}>
                  {originMeta(value).label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div className="ml-auto flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-8 text-xs"
              onClick={() => tracesQuery.refetch()}
            >
              <RefreshCw
                className={`mr-1.5 h-3 w-3 ${
                  tracesQuery.isFetching ? "animate-spin" : ""
                }`}
              />
              Refresh
            </Button>
            <Button
              size="sm"
              className="h-8 text-xs"
              onClick={() => setSimulateOpen(true)}
            >
              <Play className="mr-1.5 h-3 w-3" />
              Try a message
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 text-xs text-destructive hover:text-destructive"
              onClick={() => setConfirmClear(true)}
            >
              <Trash2 className="mr-1.5 h-3 w-3" />
              Clear
            </Button>
          </div>
        </div>

        {/* The channel filter failing is its own failure, reported next to the
            control it disabled rather than swallowed into the table below. */}
        {channelsQuery.isError && (
          <RoutingError
            error={channelsQuery.error}
            fallback="Couldn't load the channel list to filter by."
            onRetry={() => channelsQuery.refetch()}
            compact
          />
        )}

        {/* Server-authored: says an empty page means tracing is switched off,
            which must not look like "this server has never routed anything". */}
        {notice && <RoutingNotice notice={notice} />}

        {tracesQuery.isError ? (
          <RoutingError
            error={tracesQuery.error}
            fallback="Couldn't load routing decisions."
            onRetry={() => tracesQuery.refetch()}
          />
        ) : tracesQuery.isLoading ? (
          <RoutingLoading rows={5} />
        ) : rows.length === 0 ? (
          <RoutingEmpty
            title="No routing decisions match this filter."
            icon={<Compass className="h-8 w-8" />}
            hint={
              notice
                ? "See the notice above for why."
                : "Routing records a decision every time a new thread arrives. Widen the filter, or send the app a message and refresh."
            }
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8" />
                    <TableHead className="text-xs">Time</TableHead>
                    <TableHead className="text-xs">Origin</TableHead>
                    <TableHead className="text-xs">Sender</TableHead>
                    <TableHead className="text-xs">Message</TableHead>
                    <TableHead className="text-xs">Outcome</TableHead>
                    <TableHead className="text-xs">Chosen</TableHead>
                    <TableHead className="text-xs">Conf.</TableHead>
                    <TableHead className="text-xs">Provider / model</TableHead>
                    <TableHead className="text-xs">Latency</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => (
                    <TraceRow
                      key={row.id}
                      row={row}
                      expanded={expanded === row.id}
                      onToggle={() =>
                        setExpanded((current) =>
                          current === row.id ? null : row.id,
                        )
                      }
                    />
                  ))}
                </TableBody>
              </Table>
            </div>

            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                {total} decision{total === 1 ? "" : "s"} match this filter
              </p>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  disabled={page === 0}
                  onClick={() => {
                    setPage((p) => Math.max(0, p - 1))
                    setExpanded(null)
                  }}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  disabled={(page + 1) * PAGE_SIZE >= total}
                  onClick={() => {
                    setPage((p) => p + 1)
                    setExpanded(null)
                  }}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>

      <RoutingSimulateDialog
        open={simulateOpen}
        onOpenChange={setSimulateOpen}
      />

      <AlertDialog open={confirmClear} onOpenChange={setConfirmClear}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {filters.channelId
                ? "Clear this channel's routing decisions?"
                : "Clear every channel's routing decisions?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {filters.channelId
                ? "Stored decisions for the selected channel are deleted, including any message text they hold — the outcome and origin filters do not narrow this. Live routing is unaffected."
                : "Every stored routing decision on this server is deleted, for every channel and the whole retention window, including any message text they hold — the outcome and origin filters do not narrow this. Live routing is unaffected."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction asChild>
              <LoadingButton
                variant="destructive"
                loading={clearMutation.isPending}
                onClick={(event) => {
                  event.preventDefault()
                  clearMutation.mutate()
                }}
              >
                Clear
              </LoadingButton>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}

function TraceRow({
  row,
  expanded,
  onToggle,
}: {
  row: RoutingDecisionSummary
  expanded: boolean
  onToggle: () => void
}) {
  const outcome = outcomeMeta(row.outcome)
  const origin = originMeta(row.origin)
  const chosen = row.selected_agent_name ?? row.selected_bundle_name ?? null

  return (
    <>
      {/* The row stays clickable for the mouse, but the drill-down — diagnosis,
          stages, replay, recommendation — is the point of this card, so it also
          has a real focusable control. `aria-expanded` belongs on that button,
          not on a bare `<tr>`, where it is not announced. */}
      <TableRow className="cursor-pointer" onClick={onToggle}>
        <TableCell className="w-8">
          <button
            type="button"
            aria-expanded={expanded}
            aria-label={
              expanded ? "Hide decision detail" : "Show decision detail"
            }
            className="rounded p-0.5 text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            onClick={(event) => {
              // The row's own handler would otherwise toggle a second time and
              // cancel this one out.
              event.stopPropagation()
              onToggle()
            }}
          >
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
        </TableCell>
        <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
          {formatDateTime(row.created_at)}
        </TableCell>
        <TableCell>
          {/* Unknown origins render as their raw value — `simulate` is itself
              new, and the vocabulary grows without a migration. */}
          <Badge variant={origin.tone} className="text-[10px]">
            {origin.label}
          </Badge>
        </TableCell>
        <TableCell className="max-w-[12rem] truncate text-xs">
          {row.user_email || "—"}
        </TableCell>
        <TableCell className="max-w-[20rem] text-xs">
          <MessageCell row={row} />
        </TableCell>
        <TableCell>
          {/* Same convention: an unrecognised outcome keeps its own name. */}
          <Badge variant={outcome.tone} className="text-[10px]">
            {outcome.label}
          </Badge>
        </TableCell>
        <TableCell className="max-w-[12rem] truncate text-xs">
          {chosen ?? <span className="text-muted-foreground">—</span>}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {formatConfidence(row.confidence)}
        </TableCell>
        <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
          {row.provider ? `${row.provider} / ${row.model ?? "—"}` : "—"}
        </TableCell>
        <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
          {formatLatency(row.latency_ms)}
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={10} className="bg-muted/30 p-3">
            <RoutingTraceDetailLoader traceId={row.id} />
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

/**
 * The message column has three outcomes, and they are three different things:
 * text, text withheld by the message-text gate, and no text at all. The middle
 * one carries the server's notice in a tooltip rather than a truncated blob.
 */
function MessageCell({ row }: { row: RoutingDecisionSummary }) {
  // Told apart by `message_sha256`, not by the notice: the notice is set on
  // every row alike while the gate is closed, so branching on it would label a
  // decision that never carried a message as "withheld".
  const state = messageTextState(row)

  if (state === "present") {
    return <span className="line-clamp-2 break-words">{row.message_text}</span>
  }
  if (state === "withheld") {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="flex items-center gap-1 text-muted-foreground">
              <EyeOff className="h-3 w-3" />
              withheld
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-sm text-xs">
            {/* Server-authored when present; the fallback states the mechanical
                fact only (gate open now, closed at capture time). */}
            {row.message_text_notice ?? MESSAGE_WITHHELD_NO_NOTICE}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    )
  }
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="text-muted-foreground">none</span>
        </TooltipTrigger>
        <TooltipContent className="max-w-sm text-xs">
          {MESSAGE_ABSENT}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
