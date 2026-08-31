import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Download, Info, Trash2 } from "lucide-react"
import { useRef, useState } from "react"

import {
  type ImprovementRequestDetailPublic,
  ImprovementRequestsService,
  type ImprovementRequestUpdate,
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
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { downloadAuthenticatedFile, getErrorMessage } from "@/utils"
import {
  getImprovementStatusLabel,
  IMPROVEMENT_STATUSES,
  improvementShortId,
} from "@/utils/improvementRequests"

const MAX_RESOLUTION_NOTE_CHARS = 2000

/**
 * Human copy for `context.memory.unavailable_reason`
 * (`session_snapshot_service.MEMORY_REASON_*`). "Not captured" alone would be
 * read as "this install has no memory", which is a different conclusion from
 * "the container was off" — and only one of them means the recipient can stop
 * wondering about it.
 */
const MEMORY_REASON_COPY: Record<string, string> = {
  declined_by_requester: "not shared — the requester opted out",
  no_environment: "not captured — the install had no environment",
  env_not_running: "not captured — the environment was stopped",
  read_failed: "not captured — the container could not be read",
  empty: "none — this install had no memory notes",
}

type ContextBlock = Record<string, unknown>

/** One block of the frozen `context` JSON, or an empty object when absent. */
const block = (context: ContextBlock | undefined, name: string): ContextBlock =>
  (context?.[name] as ContextBlock | undefined) ?? {}

/** Render any frozen-context scalar as display text. */
const asText = (value: unknown): string | null => {
  if (value === null || value === undefined || value === "") return null
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (typeof value === "number") return String(value)
  if (typeof value === "string") return value
  return null
}

/**
 * Label one bundle revision.
 *
 * A revision with no `version` is *unversioned* (every git-origin revision is,
 * today), which is a fact about it rather than missing data — printing a bare
 * dash there reads as "we don't know". The origin is appended when it is not a
 * publish, because that is what explains an install sitting above the latest
 * published revision.
 */
const revisionLabel = (
  version: unknown,
  revisionNumber: unknown,
  origin?: unknown,
): string | null => {
  const versionText = asText(version)
  const numberText = asText(revisionNumber)
  if (!versionText && !numberText) return null
  const label = versionText
    ? numberText
      ? `${versionText} (revision ${numberText})`
      : versionText
    : `revision ${numberText} (unversioned)`
  const originText = asText(origin)
  return originText && originText !== "publish"
    ? `${label} · from ${originText}`
    : label
}

const formatDateTime = (value: string) =>
  new Date(value).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })

interface DetailRow {
  label: string
  value: string | null
}

interface ImprovementRequestDetailModalProps {
  agentId: string
  /** The request to show; `null` keeps the modal closed. */
  requestId: string | null
  onClose: () => void
}

/**
 * Read + triage surface for one improvement request: the requester's comment,
 * the frozen runtime context, the status workflow, and the archive download.
 *
 * The transcript itself is never rendered here — it only ever leaves through
 * the archive, whose cross-user downloads are audited server-side.
 */
export function ImprovementRequestDetailModal({
  agentId,
  requestId,
  onClose,
}: ImprovementRequestDetailModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  // Status and note are edited together and committed by one Save, so they
  // share one draft. It is tagged with the request it belongs to, so opening a
  // different row falls back to that row's saved values instead of carrying an
  // unsaved draft across. Derived during render — no state-sync effect.
  const [draft, setDraft] = useState<{
    requestId: string | null
    status: string
    note: string
  }>({ requestId: null, status: "", note: "" })
  const [isDownloading, setIsDownloading] = useState(false)

  const {
    data: fetched,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["improvementRequest", requestId],
    queryFn: () =>
      ImprovementRequestsService.getImprovementRequest({
        requestId: requestId as string,
      }),
    enabled: Boolean(requestId),
  })

  // Closing nulls `requestId`, which disables the query and drops its data —
  // which would flash the loading skeleton for the length of the dialog's exit
  // animation. Keep rendering the last request we saw until it is replaced.
  const lastRequestRef = useRef<ImprovementRequestDetailPublic | null>(null)
  if (fetched) lastRequestRef.current = fetched
  const request = fetched ?? (requestId ? null : lastRequestRef.current)

  const invalidateLists = () => {
    queryClient.invalidateQueries({
      queryKey: ["improvementRequests", agentId],
    })
    queryClient.invalidateQueries({
      queryKey: ["improvementRequest", requestId],
    })
  }

  const updateMutation = useMutation({
    mutationFn: (body: ImprovementRequestUpdate) =>
      ImprovementRequestsService.updateImprovementRequest({
        requestId: requestId as string,
        requestBody: body,
      }),
    // Saving is the last thing the owner does with the request, so a
    // successful save closes the dialog — same as delete. A failure keeps it
    // open with the draft intact so the edit is not lost.
    onSuccess: () => {
      showSuccessToast("Improvement request updated")
      onClose()
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Could not update the request.")),
    onSettled: invalidateLists,
  })

  const deleteMutation = useMutation({
    mutationFn: () =>
      ImprovementRequestsService.deleteImprovementRequest({
        requestId: requestId as string,
      }),
    onSuccess: () => {
      showSuccessToast("Improvement request deleted")
      onClose()
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Could not delete the request.")),
    onSettled: invalidateLists,
  })

  const handleDownload = async () => {
    if (!request) return
    setIsDownloading(true)
    try {
      await downloadAuthenticatedFile(
        `/api/v1/improvement-requests/${request.id}/archive`,
        `improvement-${improvementShortId(request.id)}.zip`,
      )
    } catch (error) {
      showErrorToast(getErrorMessage(error, "Could not download the archive."))
    } finally {
      setIsDownloading(false)
    }
  }

  const savedStatus = request?.status ?? ""
  const savedNote = request?.resolution_note ?? ""
  const isDraftForThisRequest = Boolean(
    request && draft.requestId === request.id,
  )
  const status = isDraftForThisRequest ? draft.status : savedStatus
  const resolutionNote = isDraftForThisRequest ? draft.note : savedNote
  const isDirty = status !== savedStatus || resolutionNote !== savedNote

  /** Stage an edit, seeding the untouched field from what is saved. */
  const editDraft = (patch: { status?: string; note?: string }) => {
    if (!request) return
    setDraft({
      requestId: request.id,
      status: patch.status ?? status,
      note: patch.note ?? resolutionNote,
    })
  }

  const context = request?.context as ContextBlock | undefined
  const agentBlock = block(context, "agent")
  const envBlock = block(context, "environment")
  const sdkBlock = block(context, "sdk")
  const plugins = Array.isArray(context?.plugins) ? context.plugins : []
  const promptsBlock = block(context, "prompts")
  const memoryBlock = block(context, "memory")

  // `context.prompts.diverged` is deliberately tri-state: `null` means there
  // was no installed revision to diff against, which is not the same answer as
  // "no". Rendering it as "no" would tell a publisher their text is intact
  // when nothing was ever compared. It covers the published prompt documents
  // only — the router trigger is routing metadata the platform writes by itself
  // and the install's owner may set, so counting it would flag nearly every
  // consumer install for an edit no person made.
  const divergedFields = Array.isArray(promptsBlock.diverged_fields)
    ? (promptsBlock.diverged_fields as string[])
    : []
  const promptsDiverged =
    typeof promptsBlock.diverged === "boolean"
      ? promptsBlock.diverged
        ? `yes — ${divergedFields.length > 0 ? divergedFields.join(", ") : "the install's prompts"} differ from the published revision`
        : "no — matches the published revision"
      : "no baseline to compare against"

  // Revision numbers are shared between published and git-origin revisions and
  // only a publish moves the bundle's "latest published" pointer, so an install
  // can legitimately sit above it. Both sides are labelled rather than printed
  // as bare version strings — a git revision carries no version at all, and an
  // em-dash there reads as missing data instead of "unversioned".
  const installedLabel = revisionLabel(
    agentBlock.installed_version,
    agentBlock.installed_revision_number,
    agentBlock.installed_revision_origin,
  )
  const publishedLabel = revisionLabel(
    agentBlock.latest_published_version ?? agentBlock.latest_version,
    agentBlock.latest_published_revision_number ??
      agentBlock.latest_revision_number,
  )

  const memorySummary = memoryBlock.available
    ? `${memoryBlock.file_count ?? 0} file(s) captured`
    : (MEMORY_REASON_COPY[String(memoryBlock.unavailable_reason ?? "")] ??
      "not captured")

  const detailRows: DetailRow[] = [
    // `context.agent` is the *source* install — the consumer's copy — so a
    // publisher must not read this as their own agent's name.
    { label: "Agent (as installed)", value: asText(agentBlock.name) },
    { label: "Bundle", value: asText(agentBlock.bundle_id) },
    { label: "Installed", value: installedLabel },
    { label: "Latest published", value: publishedLabel },
    { label: "Update pending", value: asText(agentBlock.update_pending) },
    { label: "Session mode", value: asText(sdkBlock.session_mode) },
    { label: "SDK engine", value: asText(sdkBlock.effective_engine) },
    { label: "Effective model", value: asText(sdkBlock.effective_model) },
    { label: "Environment", value: asText(envBlock.env_name) },
    { label: "Environment version", value: asText(envBlock.env_version) },
    { label: "Environment status", value: asText(envBlock.status_at_capture) },
    { label: "Image stale", value: asText(envBlock.image_stale) },
    { label: "Critical state", value: asText(envBlock.critical_state) },
    { label: "Plugins", value: String(plugins.length) },
    // The two blocks that answer "what was the system prompt". Only shown for
    // requests captured after prompt/memory capture existed — an older row has
    // no `prompts` block, and an empty row would read as "no prompts".
    ...(Object.keys(promptsBlock).length > 0
      ? [
          { label: "Prompts edited on the install", value: promptsDiverged },
          { label: "Personal memory", value: memorySummary },
        ]
      : []),
  ].filter((row) => row.value !== null)

  return (
    <Dialog
      open={Boolean(requestId)}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className="sm:max-w-2xl max-h-[85vh] overflow-y-auto">
        {isError ? (
          <>
            <DialogHeader>
              <DialogTitle>Improvement request</DialogTitle>
              <DialogDescription className="text-destructive">
                {getErrorMessage(error, "Couldn't load this request.")}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={onClose}>
                Close
              </Button>
            </DialogFooter>
          </>
        ) : isLoading || !request ? (
          <>
            <DialogHeader>
              <DialogTitle>Improvement request</DialogTitle>
              <DialogDescription>Loading…</DialogDescription>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-32 w-full" />
            </div>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Improvement request</DialogTitle>
              {/* Visually hidden: the dialog needs a description for
                  assistive tech, but the sighted reader gets the facts from
                  the summary block right below the title. */}
              <DialogDescription className="sr-only">
                Who shared this session, what they wrote, and how it is being
                handled.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4 py-2">
              {request.snapshot_truncated && (
                <div
                  role="note"
                  className="flex items-start gap-2 rounded-md border-l-4 border-amber-500 bg-amber-50/70 dark:bg-amber-950/30 px-3 py-2 text-sm text-amber-900 dark:text-amber-200"
                >
                  <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
                  <span>
                    This snapshot was truncated to fit the size limit — the
                    oldest messages were dropped.
                  </span>
                </div>
              )}

              {/* Who sent it, when, how much of it — and their comment, which
                  only means anything alongside those three. One block, so the
                  form below is just the decision. */}
              <div className="rounded-md border bg-muted/20">
                <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-3 px-3 py-2.5 text-sm">
                  <div className="col-span-2 min-w-0">
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      From
                    </dt>
                    <dd className="mt-0.5 truncate font-medium">
                      {request.requester_display || "Unknown"}
                    </dd>
                    {request.requester_email && (
                      <dd
                        className="truncate text-xs text-muted-foreground"
                        title={request.requester_email}
                      >
                        {request.requester_email}
                      </dd>
                    )}
                  </div>
                  <div className="min-w-0">
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Submitted
                    </dt>
                    <dd className="mt-0.5 font-medium">
                      {formatDateTime(request.created_at)}
                    </dd>
                  </div>
                  <div className="min-w-0">
                    <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                      Messages
                    </dt>
                    <dd className="mt-0.5 font-medium">
                      {request.snapshot_message_count}
                    </dd>
                  </div>
                </dl>
                {/* Requester's comment. Deliberately plain text, never
                    markdown: it is cross-user content rendered in the owner's
                    UI. */}
                <div className="border-t px-3 py-2.5">
                  <p className="text-xs uppercase tracking-wide text-muted-foreground">
                    Comment
                  </p>
                  <div className="mt-1 text-sm whitespace-pre-wrap break-words">
                    {request.comment || (
                      <span className="text-muted-foreground">
                        No comment was left.
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* Reference material and the triage decision on one line: two
                  buttons that open or fetch, and the status the owner sets. */}
              <div className="flex flex-wrap items-center gap-2">
                {/* A nested dialog rather than a popover: the list is long
                    enough that an anchored panel runs off the bottom of the
                    viewport on short screens. */}
                <Dialog>
                  <DialogTrigger asChild>
                    <Button type="button" variant="outline" size="sm">
                      <Info className="h-4 w-4" />
                      Context details
                    </Button>
                  </DialogTrigger>
                  <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto">
                    <DialogHeader>
                      <DialogTitle>Context at capture</DialogTitle>
                      <DialogDescription>
                        Frozen when the request was submitted — not a live read
                        of the install.
                      </DialogDescription>
                    </DialogHeader>
                    {detailRows.length === 0 ? (
                      <p className="text-sm text-muted-foreground">
                        No runtime context was captured with this request.
                      </p>
                    ) : (
                      <dl className="divide-y rounded-md border text-sm">
                        {detailRows.map((row) => (
                          <div
                            key={row.label}
                            className="flex items-start justify-between gap-3 px-3 py-2"
                          >
                            <dt className="text-muted-foreground shrink-0">
                              {row.label}
                            </dt>
                            <dd
                              className="font-medium text-right break-words"
                              title={row.value ?? ""}
                            >
                              {row.value}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    )}
                  </DialogContent>
                </Dialog>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={isDownloading}
                  onClick={handleDownload}
                >
                  <Download className="h-4 w-4" />
                  {isDownloading ? "Preparing…" : "Download session archive"}
                </Button>
                <div className="flex items-center gap-2 sm:ml-auto">
                  <Label
                    htmlFor="improvement-status"
                    className="text-muted-foreground"
                  >
                    Status
                  </Label>
                  {/* A draft, not an immediate PATCH: a mis-click would
                      otherwise decline a request and stamp
                      `status_changed_at` with no confirmation and no undo.
                      Saving both fields at once also makes "change the status
                      and explain why" a single round-trip. */}
                  <Select
                    value={status}
                    onValueChange={(value) => editDraft({ status: value })}
                    disabled={updateMutation.isPending}
                  >
                    <SelectTrigger
                      id="improvement-status"
                      size="sm"
                      className="w-[9.5rem]"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {IMPROVEMENT_STATUSES.map((status) => (
                        <SelectItem key={status} value={status}>
                          {getImprovementStatusLabel(status)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div>
                <div className="flex items-baseline justify-between">
                  <Label htmlFor="improvement-resolution-note">
                    Resolution note
                  </Label>
                  <span className="text-xs text-muted-foreground">
                    {resolutionNote.length}/{MAX_RESOLUTION_NOTE_CHARS}
                  </span>
                </div>
                <Textarea
                  id="improvement-resolution-note"
                  className="mt-1.5"
                  rows={3}
                  maxLength={MAX_RESOLUTION_NOTE_CHARS}
                  value={resolutionNote}
                  onChange={(e) => editDraft({ note: e.target.value })}
                />
                {/* No requester-facing view ships in this version, so the
                    caption says where the note actually goes rather than
                    promising the requester will be shown it. */}
                <p className="mt-1.5 text-xs text-muted-foreground">
                  Stored with the request. The person who submitted it can read
                  it through the API and the CLI.
                </p>
              </div>
            </div>

            <DialogFooter className="sm:justify-between">
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button
                    variant="destructive"
                    disabled={deleteMutation.isPending}
                  >
                    <Trash2 className="h-4 w-4" />
                    Delete
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>
                      Delete this improvement request?
                    </AlertDialogTitle>
                    <AlertDialogDescription>
                      The shared conversation snapshot and its archive are
                      removed permanently. The person who submitted it is not
                      notified, and they cannot send the same session again
                      without starting a new request.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction
                      onClick={() => deleteMutation.mutate()}
                      className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    >
                      Delete
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
              <div className="flex items-center gap-3">
                {isDirty && (
                  <span className="text-xs text-muted-foreground">
                    Unsaved changes
                  </span>
                )}
                <LoadingButton
                  type="button"
                  loading={updateMutation.isPending}
                  disabled={!isDirty}
                  // The note is sent verbatim, never as null: the backend
                  // treats a null note as "leave unchanged", so clearing the
                  // field has to travel as an empty string (stored as NULL).
                  onClick={() =>
                    updateMutation.mutate({
                      status,
                      resolution_note: resolutionNote,
                    })
                  }
                >
                  Save
                </LoadingButton>
              </div>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default ImprovementRequestDetailModal
