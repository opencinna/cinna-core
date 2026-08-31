import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, Info, X } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ImprovementRequestsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

const MAX_COMMENT_CHARS = 4000

const formSchema = z.object({
  comment: z
    .string()
    .max(MAX_COMMENT_CHARS, {
      message: `Keep it under ${MAX_COMMENT_CHARS} characters.`,
    })
    .optional(),
  // Mirrors `ImprovementRequestCreate.include_memory`. Defaults to true: the
  // memory area is injected into every system prompt, so leaving it out gives
  // the recipient a prompt that is not the one that ran.
  includeMemory: z.boolean(),
})

type FormData = z.infer<typeof formSchema>

/**
 * Human copy for the backend's machine-readable denial reasons
 * (`improvement_request_service.REASON_*`). The pre-flight endpoint returns the
 * code only, so the wording lives here.
 */
const REASON_COPY: Record<string, string> = {
  not_owner: "Only the owner of this session can share it.",
  not_eligible:
    "Sessions started from a guest or webapp share can't be shared — there's no identifiable account behind them to record consent against.",
  empty_session:
    "This session has no messages yet, so there's nothing to share.",
  agent_missing: "The agent this session ran on no longer exists.",
  // Two distinct backend 429s share this code — 5-per-session (permanent for
  // that session) and 20-per-day (rolls off) — so the copy must not promise
  // that waiting helps.
  rate_limited:
    "You've reached the limit of improvement requests for this session or for today.",
}

// Every line below is checked against what the backend actually captures. The
// snapshot is built by `SessionSnapshotService.capture` and masked by
// `secret_scrubber.scrub`; if either changes, this list has to change with it.
// Overstating an exclusion here is the one failure this feature cannot absorb.
const INCLUDED = [
  // `_message_entry` freezes every message, and `_tool_digest` distils each
  // agent turn's streaming events — tool names, tool inputs as JSON, result
  // text, thinking blocks and errors, 500 chars apiece.
  "Every message in this session, and what the agent did in between — the commands it ran, the files it touched, and the results it got back",
  // `_load_attachments` copies filename / mime_type / size. Bytes never move.
  "The names, types and sizes of files you attached — not what is inside them",
  // The session envelope: title, mode, status, result_state, result_summary.
  "The session's title and outcome",
  // `capture_context`: agent, environment, sdk, plugins blocks.
  "Agent, environment and model settings, and the bundle version you have installed",
  // `_prompts_block`: the four prompt documents in full, plus the tool config.
  // Always captured — there is no opt-out, so this line must be unconditional.
  "The agent's instructions — its workflow, entrypoint, refiner and trigger prompts — and which tools it may use",
  // The recipient's projection carries `requester_display` and
  // `requester_email`, and the detail modal renders both.
  "Your name and email address",
]

const NOT_INCLUDED = [
  // `_prompts_block` captures the prompt documents only. Deliberately worded
  // to claim nothing about `app-data/memory` — that is the checkbox's subject,
  // and a vaguer "workspace files" line here would read as excluding it.
  "The agent's scripts and knowledge base",
  // Deliberately narrow: `_collect_secrets` only reaches credentials linked to
  // this install, only values of 8+ characters, and only rewrites the
  // transcript's free-text fields. Anything outside that survives verbatim —
  // hence the warning line rendered under these columns.
  "Values from credentials saved on this agent — masked out of the transcript",
  "The contents of any file you attached",
  "Container logs",
  "Any of your other sessions",
]

/**
 * The full disclosure, moved off the submit path into its own layer.
 *
 * The consent screen still has to state what leaves the account, but a user
 * raising a report usually already knows — putting two lists and a warning
 * ahead of the comment box made the common case slow and the whole modal
 * scroll. The lists live here instead, one click away and reachable from the
 * footer, so the form stays a recipient line, a checkbox and a text area.
 *
 * What stays on the main modal is the part a user cannot be assumed to know:
 * who receives it, that it cannot be undone, and how much is captured.
 */
function SharingDetailsDialog({
  open,
  onOpenChange,
  includeMemory,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Live value of the form's checkbox — the lists must describe the
      submission the user is actually about to make, not a fixed one. */
  includeMemory: boolean
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>What gets shared</DialogTitle>
          <DialogDescription>
            Everything below is captured once, at the moment you submit.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 py-1">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="rounded-md border p-3">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
                Included
              </p>
              <ul className="space-y-1.5">
                {INCLUDED.map((item) => (
                  <li key={item} className="flex gap-2 text-sm">
                    <Check className="h-4 w-4 mt-0.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                    <span>{item}</span>
                  </li>
                ))}
                {/* Driven by the live checkbox: the memory area is the one
                    item whose inclusion the user controls, so a fixed list
                    would misdescribe half the submissions. */}
                <li className="flex gap-2 text-sm">
                  {includeMemory ? (
                    <Check className="h-4 w-4 mt-0.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                  ) : (
                    <X className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground" />
                  )}
                  <span
                    className={includeMemory ? "" : "text-muted-foreground"}
                  >
                    The agent&apos;s MEMORY files — the personal notes it keeps
                    about you, which go into its instructions on every message.{" "}
                    {includeMemory ? "Included" : "Left out"}, per the checkbox
                    on the form
                  </span>
                </li>
              </ul>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2">
                Not included
              </p>
              <ul className="space-y-1.5">
                {NOT_INCLUDED.map((item) => (
                  <li key={item} className="flex gap-2 text-sm">
                    <X className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground" />
                    <span className="text-muted-foreground">{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>

          {/* Masking is best-effort by construction: it can only recognise
              secrets the platform already stores for this agent. A key the user
              typed themselves is invisible to it, so the honest thing is to say
              so rather than let the exclusion column imply otherwise. */}
          <p
            role="note"
            className="text-sm rounded-md border border-amber-500/40 bg-amber-50/50 dark:bg-amber-950/20 px-3 py-2 text-amber-900 dark:text-amber-200"
          >
            Masking only covers credentials saved on this agent. Anything you
            typed into the conversation yourself — a password, an API key, a
            token pasted from somewhere else — is shared exactly as written.
            Check the conversation before you submit.
          </p>
        </div>

        <DialogFooter>
          {/* type="button": this dialog is portalled out of the parent form,
              but the default button type is still submit. */}
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
          >
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

interface ImproveAgentModalProps {
  sessionId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Called after a successful submission, so the caller can close its menu. */
  onSubmitted?: () => void
}

export function ImproveAgentModal({
  sessionId,
  open,
  onOpenChange,
  onSubmitted,
}: ImproveAgentModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [detailsOpen, setDetailsOpen] = useState(false)

  // Pre-flight. Runs the same eligibility gate and target resolution the submit
  // will run, so the disclosure below can never disagree with what the button
  // actually does. Only while the dialog is open — it is not free.
  const {
    data: context,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["improvementContext", sessionId],
    queryFn: () =>
      ImprovementRequestsService.getImprovementContext({ sessionId }),
    enabled: open,
    staleTime: 0,
  })

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onChange",
    defaultValues: { comment: "", includeMemory: true },
  })

  const commentValue = form.watch("comment") ?? ""
  const includeMemory = form.watch("includeMemory")

  /**
   * Where this request actually goes, said once, at the top.
   *
   * The three outcomes are genuinely different actions and the generic
   * "send it to whoever maintains the agent" described none of them: a bundle
   * install reaches a *publisher* the user has never met, a shared agent
   * reaches its owner, and a self-owned agent reaches nobody — it is a note to
   * self. Since the pre-flight already resolves the recipient, the header can
   * name the real one instead of hedging.
   */
  const renderRecipientLine = () => {
    if (!isExternal) {
      return (
        <>
          Kept on your own agent
          {context?.target_agent_name ? (
            <>
              {" "}
              <Badge variant="secondary" className="mx-0.5 align-middle">
                {context.target_agent_name}
              </Badge>
            </>
          ) : null}{" "}
          as a note for later improvements. Nothing leaves your account.
        </>
      )
    }
    return (
      <>
        Goes to{" "}
        <Badge variant="secondary" className="mx-0.5 align-middle">
          {context?.recipient_display ?? "the agent's owner"}
        </Badge>
        {context?.bundle_id
          ? ", who publishes the bundle this agent was installed from."
          : ", who owns this agent."}
      </>
    )
  }

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      ImprovementRequestsService.createImprovementRequest({
        requestBody: {
          session_id: sessionId,
          comment: data.comment?.trim() ? data.comment.trim() : null,
          include_memory: data.includeMemory,
        },
      }),
    onSuccess: () => {
      showSuccessToast("Improvement request submitted")
      form.reset({ comment: "", includeMemory: true })
      setDetailsOpen(false)
      onOpenChange(false)
      onSubmitted?.()
    },
    onError: (error) =>
      showErrorToast(
        getErrorMessage(error, "Could not submit the improvement request."),
      ),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["improvementRequests"] })
      queryClient.invalidateQueries({
        queryKey: ["improvementContext", sessionId],
      })
    },
  })

  const isExternal = Boolean(context?.is_shared_externally)
  const messageCount = context?.message_count ?? 0
  const existingCount = context?.existing_request_count ?? 0

  const renderBody = () => {
    if (isError) {
      return (
        <p className="text-sm text-destructive">
          {getErrorMessage(
            error,
            "Couldn't check whether this session can be shared.",
          )}
        </p>
      )
    }

    if (isLoading || !context) {
      return (
        <div className="space-y-3">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      )
    }

    if (!context.eligible) {
      return (
        <p className="text-sm text-muted-foreground">
          {REASON_COPY[context.reason ?? ""] ??
            "This session can't be shared as an improvement request."}
        </p>
      )
    }

    return (
      <div className="space-y-4">
        {/* Only the *cross-user* case still gets a callout. Who receives the
            request now lives in the dialog description for both cases, so
            repeating it here was the text overload; what is left is the part
            the description cannot carry — the exact bundle coordinates and
            the irreversibility. The self-targeted case needs no box at all:
            a bordered, muted panel directly above a Textarea read as a second
            input rather than as a notice. */}
        {isExternal && (
          <div
            role="note"
            className="rounded-md border-l-4 border-amber-500 bg-amber-50/70 dark:bg-amber-950/30 px-3 py-2.5 text-sm text-amber-900 dark:text-amber-200"
          >
            {/* The bundle id and the version are rendered as separate fields
                rather than joined, so a missing version can never turn the
                bundle id into a version label — or vice versa. */}
            {context.bundle_id ? (
              <>
                Installed from bundle{" "}
                <span className="font-mono font-medium break-all">
                  {context.bundle_id}
                </span>
                {context.installed_version ? (
                  <>
                    {" "}
                    <span className="font-medium">
                      v{context.installed_version}
                    </span>
                  </>
                ) : null}
                .{" "}
              </>
            ) : null}
            Submitting shares a copy of this conversation&apos;s messages.{" "}
            <strong>This cannot be undone.</strong>
          </div>
        )}

        {/* The memory opt-out. Personal memory is the only captured block
            that is the requester's own content rather than agent
            configuration, so it stays on the form as a decision even though
            the rest of the itemisation moved behind "Sharing details" — where
            a live row spells out what including them means. Default on: the
            memory area is part of every system prompt, and a recipient
            debugging without it is debugging the wrong prompt. */}
        <FormField
          control={form.control}
          name="includeMemory"
          render={({ field }) => (
            <FormItem className="flex flex-row items-center gap-2.5 space-y-0">
              <FormControl>
                <Checkbox
                  checked={field.value}
                  onCheckedChange={(checked) =>
                    field.onChange(checked === true)
                  }
                />
              </FormControl>
              <FormLabel className="font-normal">
                Include MEMORY files of this agent
              </FormLabel>
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name="comment"
          render={({ field }) => (
            <FormItem>
              <div className="flex items-baseline justify-between">
                <FormLabel>What went wrong? (optional)</FormLabel>
                <span className="text-xs text-muted-foreground">
                  {commentValue.length}/{MAX_COMMENT_CHARS}
                </span>
              </div>
              <FormControl>
                <Textarea
                  rows={3}
                  maxLength={MAX_COMMENT_CHARS}
                  placeholder="It kept asking for the same file over and over."
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
    )
  }

  const canSubmit = Boolean(context?.eligible) && !isLoading

  return (
    // Closing the form drops the details layer with it, so reopening the
    // modal never lands the user straight back on the disclosure.
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setDetailsOpen(false)
        onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
            className="grid gap-4"
          >
            <DialogHeader>
              <DialogTitle>Improve Agent</DialogTitle>
              {/* Where it goes and how much is captured are one thought, so
                  they are one block at one type size — as two paragraphs at
                  `text-sm` and `text-xs` they read as unrelated notes. Filled
                  but border-less, and led by an icon, so it cannot be mistaken
                  for the Textarea further down. `asChild` keeps it the
                  dialog's real `aria-describedby` target rather than a
                  decorative div beside a hidden one. */}
              {isLoading || !context?.eligible ? (
                <DialogDescription>
                  Report what went wrong so it can be fixed.
                </DialogDescription>
              ) : (
                <DialogDescription asChild>
                  <div className="flex gap-2.5 rounded-md bg-muted/60 px-3 py-2.5 text-left text-sm">
                    <Info className="mt-0.5 h-4 w-4 shrink-0" />
                    <div className="space-y-1.5">
                      <p>{renderRecipientLine()}</p>
                      <p>
                        {messageCount} message{messageCount === 1 ? "" : "s"}{" "}
                        will be captured as they are right now. Continuing the
                        conversation afterwards won&apos;t change what is
                        shared.
                        {existingCount > 0 && (
                          <>
                            {" "}
                            You have already submitted {existingCount} request
                            {existingCount === 1 ? "" : "s"} for this session.
                          </>
                        )}
                      </p>
                    </div>
                  </div>
                </DialogDescription>
              )}
            </DialogHeader>

            {renderBody()}

            <DialogFooter>
              {/* Left-aligned and beside the submit button on purpose: the
                  disclosure is off the form but must stay one click from the
                  action it describes. `type="button"` is load-bearing — the
                  shared Button sets no type, so inside a form it would
                  default to submit and fire the request. */}
              {canSubmit && (
                <Button
                  type="button"
                  variant="ghost"
                  className="sm:mr-auto"
                  onClick={() => setDetailsOpen(true)}
                >
                  <Info className="h-4 w-4" />
                  Sharing details
                </Button>
              )}
              <Button
                type="button"
                variant="outline"
                disabled={mutation.isPending}
                onClick={() => onOpenChange(false)}
              >
                {canSubmit ? "Cancel" : "Close"}
              </Button>
              {canSubmit && (
                <LoadingButton type="submit" loading={mutation.isPending}>
                  {isExternal ? "Share & submit" : "Submit"}
                </LoadingButton>
              )}
            </DialogFooter>
          </form>
        </Form>

        {/* Rendered inside the parent's content but portalled to the body by
            Radix, so it stacks above rather than nesting a form. Closing it
            returns to the form — the two layers never unmount in the same
            frame, which is what stranded `pointer-events: none` in the
            AlertDialog-in-Dialog case. */}
        <SharingDetailsDialog
          open={detailsOpen}
          onOpenChange={setDetailsOpen}
          includeMemory={includeMemory}
        />
      </DialogContent>
    </Dialog>
  )
}

export default ImproveAgentModal
