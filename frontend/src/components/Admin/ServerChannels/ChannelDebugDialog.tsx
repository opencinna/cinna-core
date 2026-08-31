import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowDownLeft,
  ArrowUpRight,
  Paperclip,
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

// ---------------------------------------------------------------------------
// Attachment skips
//
// Kept in this file rather than in `routingCopy.ts` / `channelCopy.ts` on
// purpose: the first is scoped to the Auto Routing Tuning card and says so in
// its own header, the second is form copy for the channel editor. This is the
// debug feed's vocabulary, and the feed already keeps its vocabulary here —
// `KIND_META` above is the same shape and the same fall-through rule.
//
// The backend writes these three keys into `detail` and nowhere else, so they
// are lifted out of the generic `k=v` run below and rendered as a sentence.
// Vocabulary owner: `_attachment_detail` in
// `backend/app/services/server_channels/channel_inbound_service.py`.
// ---------------------------------------------------------------------------

const ATTACHMENT_DETAIL_KEYS = new Set([
  "attachments_accepted",
  "attachments_skipped",
  "attachment_skips",
])

type SkipFamily = "refused" | "guidance" | "failed" | "other"

/**
 * Every skip code the backend can currently emit.
 *
 * A real union rather than `string`, and that is the whole point: the two maps
 * below are `Record<SkipReasonCode, …>`, so adding a code here without giving
 * it a family *and* a label is a **compile error**, not a silent demotion to
 * the "other" bucket. This file went stale exactly that way once — the
 * materialiser split `fetch_budget_exhausted` out of `timeout` long after the
 * maps were written, and nothing anywhere failed to say so.
 *
 * Mirrors the keys of `_reason_phrase` in
 * `backend/app/services/server_channels/channel_inbound_service.py`. It is a
 * hand-kept mirror of a Python dict — no generated client carries these, since
 * they travel as free text inside an event's `detail` — so it can still drift
 * *behind* the backend. What it can no longer do is drift internally, and the
 * runtime fall-throughs below cover the rest.
 */
type SkipReasonCode =
  | "too_large"
  | "type_not_allowed"
  | "too_many_attachments"
  | "aggregate_limit"
  | "quota_exceeded"
  | "fetch_budget_exhausted"
  | "timeout"
  | "not_found"
  | "forbidden"
  | "drive_file"
  | "no_content"
  | "storage_error"
  | "upstream_error"
  | "invalid_handle"
  | "poll_budget_exhausted"

/**
 * Which failure family a skip code belongs to.
 *
 * The split is not cosmetic: an admin's next action differs per family, and
 * collapsing them into one "skipped" count would cost the feed its whole
 * diagnostic value (plan §10).
 *
 * **Three families, not two, and the discrepancy is deliberate.** Plan §10
 * names two ("refused by validation" and "failed to fetch") and lists
 * `drive_file` under the second. The implemented backend does not agree with
 * its own plan there: `_reason_phrase` in `channel_inbound_service.py` splits
 * its phrases into three commented groups, the third being "Guidance, not just
 * a refusal (§6.5)" — `drive_file` and `poll_budget_exhausted`. That grouping
 * is the better one and this file follows it, because §10's stated *purpose*
 * for the split is whose action comes next, and on those two codes the
 * two-family taxonomy answers it wrongly: nothing broke on a Drive link, so
 * filing it under "the operator's to look at" sends an admin hunting an
 * infrastructure fault that does not exist. Same for a mail-poll budget, which
 * the very next tick clears by itself.
 *
 * **`fetch_budget_exhausted` is `guidance`, decided on that same test.** Three
 * sources disagreed about it first — the backend's comment divider files it
 * under "failed to fetch or store", plan §10 reads as refused, and this file
 * says guidance — so the rule that decided it is recorded here rather than the
 * verdict alone, because the verdict alone is what let it move three times.
 *
 * **`drive_file` is the governing precedent, not an exception to argue
 * around.** It asks the sender to change what they send, nothing broke, and it
 * sits in guidance. `fetch_budget_exhausted` has exactly that shape: the fetch
 * was still queued on the concurrency limiter and never issued a request, so
 * there is no fault anywhere, and sending fewer files at once is the sender's
 * own next action. The negative half is what really settles it and it is the
 * test this whole taxonomy encodes — filing this as anything an admin should
 * look at sends them hunting an infrastructure fault that does not exist.
 *
 * A competing "a *different* message means refused" test was proposed from
 * outside this file. It is not the rule the implemented taxonomy uses, and
 * `drive_file` is the standing proof: it too needs a different message, and it
 * is guidance. Prefer the precedent when the next borderline code arrives.
 *
 * Vocabulary owners: `REASON_*` in `services/files/attachment_limits.py` and
 * `services/server_channels/channel_attachment_service.py`, plus each
 * adapter's own fetch failures — enumerated as `SkipReasonCode` above, which
 * this map must cover exhaustively.
 *
 * A code that is not here is **not** guessed into a family. It falls to
 * `"other"` and is rendered under a neutral heading with the code shown
 * verbatim — a transport added later must not be able to silently erase its
 * own reason, nor have it misattributed to the wrong party.
 */
const SKIP_REASON_FAMILY: Record<SkipReasonCode, SkipFamily> = {
  // ---- Refused by validation: the sender's to fix ----
  too_large: "refused",
  type_not_allowed: "refused",
  aggregate_limit: "refused",
  quota_exceeded: "refused",
  too_many_attachments: "refused",
  // ---- Guidance, not a fault: the sender's own next action clears it ----
  drive_file: "guidance",
  poll_budget_exhausted: "guidance",
  // Not "failed": nothing broke. The fetch was still queued on the
  // concurrency limiter when the deadline hit and never issued a request, so
  // there is no infrastructure fault for an admin to chase. Same shape as
  // `drive_file` above — the sender changes what they send and it works.
  //
  // **Distinct from `poll_budget_exhausted` despite the rhyming names**, and
  // worth keeping straight even though both land here: they are both "a
  // budget ran out", but the poll one is cleared by re-sending the *same*
  // message once the next tick brings a fresh budget, while this one is not —
  // the same message spends the same whole-message fetch budget on the same
  // files and exhausts it again. It takes a *smaller* message. Same family,
  // different remedy; do not merge the two codes or their labels.
  fetch_budget_exhausted: "guidance",
  // ---- Failed to fetch or store: the operator's ----
  timeout: "failed",
  not_found: "failed",
  forbidden: "failed",
  no_content: "failed",
  upstream_error: "failed",
  storage_error: "failed",
  invalid_handle: "failed",
}

/**
 * Short label for a skip code.
 *
 * Deliberately terse — the sender-facing prose lives on the backend
 * (`_reason_phrase`) and is a different register. Here the reader is a
 * superuser diagnosing a channel, so the label names the mechanism.
 *
 * **An unknown code renders as itself**, never as blank and never as
 * "unknown": the exact token is the thing an operator greps the backend for,
 * and it is the only surface where the raw code is shown at all.
 */
const SKIP_REASON_LABELS: Record<SkipReasonCode, string> = {
  too_large: "too large",
  type_not_allowed: "type not allowed",
  aggregate_limit: "message total too large",
  quota_exceeded: "sender storage full",
  too_many_attachments: "too many attachments",
  timeout: "download timed out",
  not_found: "no longer available",
  forbidden: "download not permitted",
  drive_file: "Google Drive link",
  no_content: "no readable content",
  upstream_error: "download failed",
  storage_error: "could not be saved",
  invalid_handle: "unusable reference",
  poll_budget_exhausted: "mail poll budget exhausted",
  // Names the concurrency cap, not the network: unlike `timeout`, this fetch
  // was still queued and never issued a request.
  fetch_budget_exhausted: "too many files to download at once",
}

/**
 * The two maps above are keyed by `SkipReasonCode` so that a code added to
 * the union without an entry fails the build. What arrives at runtime is a
 * plain `string` off the wire, though, and it may well *not* be in the union:
 * this bundle can be older or newer than the backend serving it.
 *
 * So the reads are widened deliberately, in one place, rather than by
 * loosening the maps to `Record<string, …>` — that would trade the build-time
 * guarantee away to buy something these two accessors already provide. The
 * type catches the author who forgets a code; the `??` catches the deploy
 * where backend and frontend disagree. Both are needed, and neither
 * substitutes for the other.
 */
const SKIP_REASON_LABEL_LOOKUP: Record<string, string | undefined> =
  SKIP_REASON_LABELS
const SKIP_REASON_FAMILY_LOOKUP: Record<string, SkipFamily | undefined> =
  SKIP_REASON_FAMILY

function skipReasonLabel(reason: string): string {
  return SKIP_REASON_LABEL_LOOKUP[reason] ?? reason
}

/** Unknown codes land in `"other"`, never guessed into a real family. */
function skipReasonFamily(reason: string): SkipFamily {
  return SKIP_REASON_FAMILY_LOOKUP[reason] ?? "other"
}

const FAMILY_META: Record<SkipFamily, { label: string; hint: string }> = {
  refused: { label: "Refused", hint: "the sender's to fix" },
  guidance: {
    label: "Needs resending",
    hint: "the sender's to redo — nothing broke",
  },
  failed: { label: "Failed", hint: "the operator's to look at" },
  // No family claimed, because none is known. Says only what happened.
  other: { label: "Skipped", hint: "unrecognised reason" },
}

/** Families in the order they are rendered — sender-side first, then ours. */
const FAMILY_ORDER: SkipFamily[] = ["refused", "guidance", "failed", "other"]

interface AttachmentSkip {
  /** `null` when the entry did not parse — `reason` then holds it verbatim. */
  filename: string | null
  reason: string
}

interface AttachmentInfo {
  accepted: number
  /** Authoritative: from `attachments_skipped`, which is never truncated. */
  skipped: number
  /** How many of those the (capped) list actually managed to name. */
  listed: number
  /** The list was cut short by the backend's character cap. */
  truncated: boolean
  groups: { family: SkipFamily; items: AttachmentSkip[] }[]
}

/** `"report.mp4 (too_large)"` → filename + code. */
const SKIP_ENTRY_RE = /^(.+)\s\(([^()]+)\)$/

/**
 * The backend's truncation marker — it slices to 499 chars and appends this.
 * See `_MAX_SKIP_DETAIL_CHARS` in `channel_inbound_service.py`.
 */
const SKIP_TRUNCATION_MARKER = "…"

/**
 * Read `attachment_skips` back into entries.
 *
 * The backend flattens the list into one capped string because
 * `ChannelDebugEvent.detail` is typed `dict[str, str]` and widening it would
 * have cost this feature its "no client regeneration" property (plan §6.2).
 * So this parse is best-effort by construction, and it is written to be
 * **total** — there is no input for which it throws, yields `undefined`, or
 * silently loses a row:
 *
 * - Filenames are sender-supplied text on an unauthenticated ingress and may
 *   contain `; ` or parentheses. A segment that does not match is carried
 *   through verbatim rather than dropped — a mangled row is readable, a
 *   missing one is a lie.
 * - **The cap is not entry-aware.** It cuts at a character count, so on a long
 *   list the final segment can be a fragment with no `(reason)` at all, or
 *   half a filename. That fragment is dropped rather than rendered as if it
 *   were a whole entry (a stray `report.m` reads as a real, differently-named
 *   file), and `truncated` is set so the view can say the list was cut. This
 *   is exactly the many-skips case an admin most wants to read correctly:
 *   showing 4 of 9 with no marker invites the wrong conclusion about the
 *   other 5.
 */
function parseSkips(raw: string | undefined): {
  items: AttachmentSkip[]
  truncated: boolean
} {
  if (!raw) return { items: [], truncated: false }
  const truncated = raw.endsWith(SKIP_TRUNCATION_MARKER)
  const body = truncated ? raw.slice(0, -SKIP_TRUNCATION_MARKER.length) : raw
  const segments = body
    .split("; ")
    .map((entry) => entry.trim())
    .filter(Boolean)

  const items: AttachmentSkip[] = []
  segments.forEach((entry, index) => {
    const match = SKIP_ENTRY_RE.exec(entry)
    if (match) {
      items.push({ filename: match[1], reason: match[2] })
      return
    }
    // Unmatched AND last AND the string was cut: this is the fragment the cap
    // left behind, not a filename that happens to contain `; `. Drop it; the
    // truncation notice below accounts for it.
    if (truncated && index === segments.length - 1) return
    items.push({ filename: null, reason: entry })
  })
  return { items, truncated }
}

/** Non-negative integer, or 0. The counts arrive stringified. */
function toCount(raw: string | undefined): number {
  const value = Number.parseInt(raw ?? "", 10)
  return Number.isFinite(value) && value > 0 ? value : 0
}

/**
 * The attachment facts in one event's `detail`, or `null` when it carries
 * none.
 *
 * `null` is the common case and it matters: a message that had no attachments
 * must render exactly as it did before this feature existed — no empty line,
 * no "0 files", no layout shift. The keys are absent altogether on such a
 * message, so their absence is the gate.
 */
function readAttachments(
  detail: { [key: string]: string } | null | undefined,
): AttachmentInfo | null {
  if (!detail) return null
  const hasCounts =
    detail.attachments_accepted !== undefined ||
    detail.attachments_skipped !== undefined
  if (!hasCounts) return null

  const { items, truncated } = parseSkips(detail.attachment_skips)
  const groups = FAMILY_ORDER.map((family) => ({
    family,
    items: items.filter(
      (skip) => skipReasonFamily(skip.reason) === family,
    ),
  })).filter((group) => group.items.length > 0)

  return {
    accepted: toCount(detail.attachments_accepted),
    // `attachments_skipped` is the authority for "how many": it is a plain
    // count and is never truncated, whereas `attachment_skips` is capped and
    // routinely names fewer. `max` only guards the case where the two keys —
    // which the backend writes together, one line apart — somehow disagree;
    // under-reporting a failure is the wrong direction to fail in.
    skipped: Math.max(toCount(detail.attachments_skipped), items.length),
    listed: items.length,
    truncated,
    groups,
  }
}

function AttachmentSummary({ info }: { info: AttachmentInfo }) {
  return (
    <div className="space-y-0.5">
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Paperclip className="h-3 w-3 shrink-0" />
        <span>
          {info.accepted} {info.accepted === 1 ? "file" : "files"}
          {info.skipped > 0 && ` · ${info.skipped} skipped`}
        </span>
      </p>
      {info.groups.map((group) => (
        <p
          key={group.family}
          className="pl-[18px] text-[11px] text-muted-foreground break-words"
        >
          <span className="font-medium">{FAMILY_META[group.family].label}</span>{" "}
          ({FAMILY_META[group.family].hint}):{" "}
          {group.items
            .map((item) =>
              item.filename
                ? `${item.filename} (${skipReasonLabel(item.reason)})`
                : item.reason,
            )
            .join(", ")}
        </p>
      ))}
      {/* Says the list is partial. Without it an admin reads 4 named skips
          beside a count of 9 and concludes the other 5 vanished somewhere in
          the pipeline, when they were only cut from this one string. */}
      {info.truncated && (
        <p className="pl-[18px] text-[11px] text-muted-foreground italic">
          {info.skipped > info.listed
            ? `Showing ${info.listed} of ${info.skipped} — the rest were cut from this record.`
            : "This list was cut short by the record's length limit."}
        </p>
      )}
    </div>
  )
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
  // Attachment keys are rendered as a sentence below, so they are lifted out
  // of the generic `k=v` run rather than shown twice.
  const detail = Object.entries(event.detail ?? {}).filter(
    ([k, v]) => v !== "" && !ATTACHMENT_DETAIL_KEYS.has(k),
  )
  const attachments = readAttachments(event.detail)

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

      {attachments && <AttachmentSummary info={attachments} />}

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
            pipeline decided about each message. Held in memory only — a backend
            restart clears it.
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
              Send the app a message from{" "}
              {channel.channel_type.replace("_", " ")} — it appears here within
              a few seconds, along with what the pipeline decided to do with it.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {events.map((event) => (
              <DebugEventRow
                key={event.id}
                event={event}
                sending={testMutation.isPending && sendingEventId === event.id}
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
