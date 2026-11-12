import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import {
  AlertTriangle,
  Download,
  FileCode,
  Hammer,
  Info,
  Layers,
  Trash2,
} from "lucide-react"
import { type ReactNode, useRef, useState } from "react"

import {
  type ImprovementRequestDetailPublic,
  ImprovementRequestsService,
  type ImprovementRequestUpdate,
  SessionsService,
} from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  SEGMENTED_DIALOG_PANEL_CLASS,
  SEGMENTED_LIST_CLASS,
  SEGMENTED_TRIGGER_CLASS,
} from "@/components/Common/SegmentedTabs"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { downloadAuthenticatedFile, getErrorMessage } from "@/utils"
import {
  getImprovementStatusLabel,
  hasEndpointDetails,
  IMPROVEMENT_STATUSES,
  improvementShortId,
  isApiCapabilityRequest,
  readApiRequest,
} from "@/utils/improvementRequests"
import { ImprovementRequestApiEndpoint } from "./ImprovementRequestApiEndpoint"
import { ImprovementRequestFacts } from "./ImprovementRequestFacts"
import { ImprovementRequestSessionContext } from "./ImprovementRequestSessionContext"

const MAX_RESOLUTION_NOTE_CHARS = 2000

/** The building agent's first message for an API capability request (D11). */
const buildingSessionMessage = (requestId: string) =>
  `Handle API capability request ${requestId}: read it with api_requests_get, extend the API following the REST API building guide, then close it with api_requests_update_status.`

interface ImprovementRequestDetailModalProps {
  agentId: string
  /** The request to show; `null` keeps the modal closed. */
  requestId: string | null
  onClose: () => void
}

/**
 * Read + triage surface for one improvement request.
 *
 * Two kinds. A **session** request carries the requester's comment and the
 * runtime context frozen at submission (the Context tab); its transcript only
 * ever leaves through the archive, whose cross-user downloads are audited
 * server-side. An **API capability request** carries what a caller of the
 * agent's REST API asked for; its proposed endpoint and examples sit on the
 * Endpoint tab, and "Start building session" hands it to the building agent.
 *
 * Facts and the document are split by tabs (§2 Facts and a document); the
 * triage draft lives here, so it survives a tab switch.
 */
export function ImprovementRequestDetailModal({
  agentId,
  requestId,
  onClose,
}: ImprovementRequestDetailModalProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
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
  const [deleteOpen, setDeleteOpen] = useState(false)

  const {
    data: fetched,
    isLoading,
    isError,
    error,
    refetch,
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

  // Closing drops the triage draft: the modal stays mounted in the card, so
  // a kept draft would reappear on the next open as "Unsaved changes" over
  // whatever the server holds by then.
  const close = () => {
    setDraft({ requestId: null, status: "", note: "" })
    onClose()
  }

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
      close()
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
      setDeleteOpen(false)
      // Gone: drop it rather than re-read it into a 404.
      queryClient.removeQueries({ queryKey: ["improvementRequest", requestId] })
      close()
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Could not delete the request.")),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: ["improvementRequests", agentId],
      }),
  })

  const buildMutation = useMutation({
    mutationFn: async (target: ImprovementRequestDetailPublic) => {
      const session = await SessionsService.createSession({
        requestBody: { agent_id: target.target_agent_id, mode: "building" },
      })
      if (target.status === "new") {
        // The session exists either way, so a failed status write is told,
        // not fatal.
        try {
          await ImprovementRequestsService.updateImprovementRequest({
            requestId: target.id,
            requestBody: { status: "in_progress" },
          })
        } catch (error) {
          showErrorToast(
            getErrorMessage(error, "Could not mark the request in progress."),
          )
        }
      }
      return { sessionId: session.id, requestId: target.id }
    },
    onSuccess: ({ sessionId, requestId: id }) => {
      close()
      navigate({
        to: "/session/$sessionId",
        params: { sessionId },
        search: {
          initialMessage: buildingSessionMessage(id),
          fileIds: undefined,
          fileObjects: undefined,
          pageContext: undefined,
        },
      })
    },
    onError: (error) =>
      showErrorToast(
        getErrorMessage(error, "Could not start a building session."),
      ),
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

  const isBusy =
    updateMutation.isPending ||
    deleteMutation.isPending ||
    buildMutation.isPending ||
    isDownloading

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

  const isApi = request ? isApiCapabilityRequest(request) : false
  const payload = request && isApi ? readApiRequest(request.context) : null
  const apiTitle = payload
    ? request?.title || payload.title || "Untitled API request"
    : ""
  const consumerAgent = payload
    ? request?.source_agent_name || payload.consumerAgentName
    : ""

  const renderBody = (req: ImprovementRequestDetailPublic): ReactNode => {
    const details = (
      <>
        {req.snapshot_truncated && (
          <Alert className="border-warning text-warning">
            <AlertTriangle />
            <AlertDescription>
              This snapshot was truncated to fit the size limit — the oldest
              messages were dropped.
            </AlertDescription>
          </Alert>
        )}

        <ImprovementRequestFacts request={req} payload={payload} />

        {/* Reference material and the triage decision on one line. */}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={isBusy}
            onClick={handleDownload}
          >
            <Download className="h-4 w-4" />
            {isDownloading
              ? "Preparing…"
              : isApi
                ? "Download request archive"
                : "Download session archive"}
          </Button>
          {isApi && (
            <StartBuildingButton
              isDirty={isDirty}
              loading={buildMutation.isPending}
              disabled={isBusy}
              onClick={() => buildMutation.mutate(req)}
            />
          )}
          <div className="flex items-center gap-2 sm:ml-auto">
            <Label
              htmlFor="improvement-status"
              className="text-muted-foreground"
            >
              Status
            </Label>
            {/* A draft, not an immediate PATCH: a mis-click would otherwise
                decline a request and stamp `status_changed_at` with no
                confirmation and no undo. */}
            <Select
              value={status}
              onValueChange={(value) => editDraft({ status: value })}
              disabled={isBusy}
            >
              <SelectTrigger
                id="improvement-status"
                size="sm"
                className="w-[9.5rem]"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {IMPROVEMENT_STATUSES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {getImprovementStatusLabel(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div>
          <div className="flex items-baseline justify-between">
            <Label htmlFor="improvement-resolution-note">Resolution note</Label>
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
            disabled={isBusy}
            onChange={(e) => editDraft({ note: e.target.value })}
          />
          {/* No requester-facing view ships in this version, so the caption
              says where the note actually goes. */}
          <p className="mt-1.5 text-xs text-muted-foreground">
            Stored with the request. The person who submitted it can read it
            through the API and the CLI.
          </p>
        </div>
      </>
    )

    const second = payload
      ? hasEndpointDetails(payload)
        ? {
            value: "endpoint",
            label: "Endpoint",
            icon: FileCode,
            body: <ImprovementRequestApiEndpoint payload={payload} />,
          }
        : null
      : {
          value: "context",
          label: "Context",
          icon: Layers,
          body: <ImprovementRequestSessionContext request={req} />,
        }

    if (!second) {
      return <div className={SEGMENTED_DIALOG_PANEL_CLASS}>{details}</div>
    }
    const SecondIcon = second.icon
    return (
      <Tabs defaultValue="details" className="min-w-0 gap-4">
        <TabsList className={SEGMENTED_LIST_CLASS}>
          <TabsTrigger value="details" className={SEGMENTED_TRIGGER_CLASS}>
            <Info className="h-3.5 w-3.5" />
            Details
          </TabsTrigger>
          <TabsTrigger value={second.value} className={SEGMENTED_TRIGGER_CLASS}>
            <SecondIcon className="h-3.5 w-3.5" />
            {second.label}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="details" className={SEGMENTED_DIALOG_PANEL_CLASS}>
          {details}
        </TabsContent>
        <TabsContent
          value={second.value}
          className={SEGMENTED_DIALOG_PANEL_CLASS}
        >
          {second.body}
        </TabsContent>
      </Tabs>
    )
  }

  return (
    <Dialog
      open={Boolean(requestId)}
      onOpenChange={(open) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!open && !isBusy) close()
      }}
    >
      <DialogContent className="sm:max-w-2xl max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        {isError ? (
          <>
            <DialogHeader>
              <DialogTitle>Improvement request</DialogTitle>
              <DialogDescription className="sr-only">
                This request could not be loaded.
              </DialogDescription>
            </DialogHeader>
            <QueryErrorAlert
              error={error}
              fallback="Couldn't load this request"
              onRetry={() => refetch()}
            />
            <DialogFooter>
              <Button variant="outline" onClick={close}>
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
              {isApi ? (
                <>
                  <DialogTitle className="break-words">{apiTitle}</DialogTitle>
                  <DialogDescription className="break-words">
                    Capability request from{" "}
                    {request.requester_display || "an unknown caller"}
                    {consumerAgent ? ` through ${consumerAgent}` : ""}.
                  </DialogDescription>
                </>
              ) : (
                <>
                  <DialogTitle>Improvement request</DialogTitle>
                  {/* Visually hidden: the sighted reader gets the facts from
                      the summary block right below the title. */}
                  <DialogDescription className="sr-only">
                    Who shared this session, what they wrote, and how it is
                    being handled.
                  </DialogDescription>
                </>
              )}
            </DialogHeader>

            {renderBody(request)}

            <DialogFooter className="sm:justify-between">
              <Button
                variant="destructive"
                disabled={isBusy}
                onClick={() => setDeleteOpen(true)}
              >
                <Trash2 className="h-4 w-4" />
                Delete
              </Button>
              <div className="flex items-center gap-3">
                {isDirty && (
                  <span className="text-xs text-muted-foreground">
                    Unsaved changes
                  </span>
                )}
                <LoadingButton
                  type="button"
                  loading={updateMutation.isPending}
                  disabled={!isDirty || isBusy}
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

            <AlertDialog
              open={deleteOpen}
              onOpenChange={(next) => {
                if (!deleteMutation.isPending) setDeleteOpen(next)
              }}
            >
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle className="break-words">
                    {isApi
                      ? `Delete the request "${apiTitle}"?`
                      : "Delete this improvement request?"}
                  </AlertDialogTitle>
                  <AlertDialogDescription>
                    {isApi
                      ? "The request and its archive are removed permanently. The caller is not notified."
                      : "The shared conversation snapshot and its archive are removed permanently. The person who submitted it is not notified, and they cannot send the same session again without starting a new request."}
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel disabled={deleteMutation.isPending}>
                    Cancel
                  </AlertDialogCancel>
                  <LoadingButton
                    variant="destructive"
                    loading={deleteMutation.isPending}
                    onClick={() => deleteMutation.mutate()}
                  >
                    Delete
                  </LoadingButton>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

/**
 * "Start building session" — disabled while the triage draft is unsaved,
 * because the action navigates away and would drop it. A disabled button
 * fires no pointer events, so the tooltip sits on a wrapping `span`.
 */
function StartBuildingButton({
  isDirty,
  loading,
  disabled,
  onClick,
}: {
  isDirty: boolean
  loading: boolean
  disabled: boolean
  onClick: () => void
}) {
  const button = (
    <LoadingButton
      type="button"
      variant="outline"
      size="sm"
      loading={loading}
      disabled={disabled || isDirty}
      onClick={onClick}
    >
      {!loading && <Hammer className="h-4 w-4" />}
      {loading ? "Starting…" : "Start building session"}
    </LoadingButton>
  )
  if (!isDirty) return button
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* biome-ignore lint/a11y/noNoninteractiveTabindex: the wrapper carries the disabled button's tooltip for keyboard users too. */}
        <span tabIndex={0}>{button}</span>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Save or discard your changes first
      </TooltipContent>
    </Tooltip>
  )
}

export default ImprovementRequestDetailModal
