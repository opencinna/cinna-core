/**
 * GitPullConflictDialog — "what would this pull overwrite, and what do I do?"
 *
 * Replaces the dead-end 409 toast ("push or discard first" — one impossible,
 * one nonexistent) with the change list the backend already computes, plus the
 * two resolutions the pull endpoint accepts.
 *
 * Reached two ways, both rendering the same body:
 *  - pre-emptively, when the card sees `update_available && dirty` and offers
 *    "Review & pull" instead of a Pull that is guaranteed to fail;
 *  - reactively, from the recoverable 409 (`detail.code === "local_changes"`)
 *    a race can still produce — seeded from `detail.blocking` until the status
 *    query resolves.
 */
import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertTriangle, Loader2, ShieldCheck } from "lucide-react"

import { AgentGitService, type GitCommit } from "@/client"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import { Separator } from "@/components/ui/separator"
import { ChangeGroup, ChangeRow } from "./GitChangeList"
import { GitDiffDialog, type GitDiffTarget } from "./GitDiffDialog"

/** The `detail.blocking` entries a `local_changes` 409 carries. */
export interface GitBlockingChange {
  section: string
  field: string
  /** Raw attribute name — the diff endpoint's key. Absent on older payloads. */
  key?: string
  change_type: string
}

export type GitPullResolution = "keep_local" | "take_remote"

/**
 * Read `detail.blocking` off a recoverable `local_changes` 409. Returns `[]`
 * for any other error shape, so callers can use it unguarded.
 */
export function localChangesBlocking(error: unknown): GitBlockingChange[] {
  const detail = (error as { body?: { detail?: unknown } } | null)?.body?.detail
  if (!detail || typeof detail !== "object") return []
  const blocking = (detail as { blocking?: unknown }).blocking
  if (!Array.isArray(blocking)) return []
  return blocking.filter(
    (entry): entry is GitBlockingChange =>
      !!entry && typeof entry === "object" && typeof entry.field === "string",
  )
}

interface GitPullConflictDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  agentId: string
  agentName: string
  /** Remote HEAD from `check-updates`; used to name the incoming commit. */
  remoteCommit?: string | null
  /** Already-loaded recent commits — the incoming SHA's message, when known. */
  commits: GitCommit[]
  /** Blocking list seeded from a 409, shown until the status query resolves. */
  seededBlocking?: GitBlockingChange[]
  /** `undefined` ⇒ plain bodiless pull (offered only when nothing blocks). */
  onResolve: (resolution?: GitPullResolution) => void
  isPending: boolean
}

export function GitPullConflictDialog({
  open,
  onOpenChange,
  agentId,
  agentName,
  remoteCommit,
  commits,
  seededBlocking = [],
  onResolve,
  isPending,
}: GitPullConflictDialogProps) {
  // Which resolution is awaiting confirmation. BOTH are confirmed, not just the
  // destructive one: "keep my changes" still replaces the whole workspace tree
  // and leaves the agent dirty, so it is no more a casual click than the other.
  const [pendingResolution, setPendingResolution] =
    useState<GitPullResolution | "plain" | null>(null)
  // The row whose diff is open, if any.
  const [diffTarget, setDiffTarget] = useState<GitDiffTarget | null>(null)
  useEffect(() => {
    if (!open) {
      setPendingResolution(null)
      setDiffTarget(null)
    }
  }, [open])

  /**
   * Diff opener for a blocking row. Older 409 payloads carry no `key`, and
   * without one the endpoint cannot be addressed — return undefined so the row
   * renders as plain text rather than a button that would 400.
   */
  const diffOpener = (c: GitBlockingChange) =>
    c.key
      ? () =>
          setDiffTarget({
            section: c.section,
            key: c.key as string,
            label: c.field,
          })
      : undefined

  // Same gating as the commit preview: the status read snapshots + diffs the
  // whole workspace, so only fetch it while the dialog is actually open.
  // `staleTime: 0` (vs. the card's 30s) is deliberate and observer-local — a
  // destructive choice must be made against current truth, not a cached read
  // taken before whatever just changed.
  const {
    data: status,
    isFetching: isFetchingStatus,
    isError: isStatusError,
  } = useQuery({
    queryKey: ["git-status", agentId],
    queryFn: () => AgentGitService.getGitStatus({ agentId }),
    enabled: open,
    staleTime: 0,
    // The status read can fail loud (503) on a lost baseline. Don't retry-loop
    // it behind a disabled dialog — surface it as an error state straight away.
    retry: false,
  })

  // `staleTime: 0` forces a refetch on open, but React Query still SERVES the
  // cached value meanwhile — and the card's commit preview keeps this same key
  // warm for 30s. Acting on that stale read is exactly the bug this dialog
  // exists to prevent: on the reactive-409 path a cached "clean" status would
  // hide the 409's own blocking list, offer a plain Pull, and 409 again in a
  // loop. So nothing counts as truth until a fetch settles while we are open.
  // `isError` is part of the gate too: a FAILED refetch leaves `isFetching`
  // false with the stale `status` still in cache, which would otherwise read as
  // fresh — the one case where "not loading" and "current" come apart.
  const isFresh = !!status && !isFetchingStatus && !isStatusError

  // A seeded 409 always WINS over the derived list. It is the server's own
  // guard answering the pull we just attempted, so it is strictly more current
  // than any status read — including one that settled a moment ago.
  //
  // This is what closes the loop when a 409 arrives while the dialog is ALREADY
  // open (a building-mode session rewrites a prompt after a clean status read):
  // `setPullConflictOpen(true)` is then a no-op, so `isFresh` would still be
  // true and the stale "nothing blocks" + plain Pull would 409 again forever.
  const hasSeeded = seededBlocking.length > 0

  const blocking: GitBlockingChange[] = isFresh && !hasSeeded
    ? [
        ...(status.prompt_changes ?? [])
          .filter((c) => c.blocks_pull)
          .map((c) => ({
            section: "prompt",
            field: c.field,
            key: c.key,
            change_type: c.change_type,
          })),
        ...(status.setting_changes ?? [])
          .filter((c) => c.blocks_pull)
          .map((c) => ({
            // Same vocabulary the 409's detail.blocking uses, so both seeds of
            // this list mean the same thing. Only `metadata` can ever block.
            section: "metadata",
            field: c.field,
            key: c.key,
            change_type: c.change_type,
          })),
      ]
    : seededBlocking

  const promptBlocking = blocking.filter((c) => c.section === "prompt")
  const settingBlocking = blocking.filter((c) => c.section !== "prompt")
  const files = isFresh ? status.file_changes ?? [] : []

  // The card opens this dialog on the broad `dirty` signal, which also covers
  // changes a pull does NOT block on (workspace files, schedules, SDK). Once a
  // FRESH status read confirms nothing blocks, offer a plain pull rather than
  // two resolutions for a conflict that does not exist — the workspace
  // replacement warning is still worth showing, which is why we do not auto-pull.
  const nothingBlocks = isFresh && !hasSeeded && blocking.length === 0
  // Actions need a basis: a fresh status read, or a seeded 409. Without either
  // (the pre-emptive path still loading, or a status read that failed with
  // nothing seeded) there is nothing to decide on.
  const actionsDisabled = isPending || (!isFresh && !hasSeeded)

  const incoming = remoteCommit
    ? commits.find((c) => c.sha === remoteCommit)
    : undefined
  const incomingSha = incoming?.short_sha ?? remoteCommit?.slice(0, 7)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Pull with local changes</DialogTitle>
          <DialogDescription>
            The remote has new commits for {agentName}, but this agent has local
            changes the pull would replace. Choose what to keep.
          </DialogDescription>
        </DialogHeader>

        {isStatusError ? (
          <p className="flex items-start gap-1.5 text-xs text-destructive">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>
              {hasSeeded
                ? "Could not load the full change list — including which " +
                  "workspace files this pull would replace. Below is what the " +
                  "server reported when it blocked the pull."
                : "Could not load the change list for this agent. Close and " +
                  "retry, or use Refresh on the card."}
            </span>
          </p>
        ) : !isFresh ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Computing changes…
          </div>
        ) : null}

        <div className="space-y-3 rounded-md border bg-muted/40 p-3 max-h-72 overflow-y-auto">
          {incomingSha && (
            <div className="space-y-1">
              <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
                Incoming
              </p>
              <p className="text-xs">
                <span className="font-mono">{incomingSha}</span>
                {incoming?.message ? ` · ${incoming.message}` : null}
              </p>
            </div>
          )}

          {blocking.length > 0 && (
            <>
              {incomingSha && <Separator />}
              <div className="space-y-2">
                <p className="flex items-center gap-1.5 text-xs font-medium text-amber-700 dark:text-amber-400">
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                  Blocks the pull
                </p>
                <p className="text-[11px] text-muted-foreground">
                  These would be overwritten by the remote version. They are
                  often written by the agent itself during a building-mode
                  session, not edited by hand.
                </p>
                {promptBlocking.length > 0 && (
                  <ChangeGroup title="Prompts">
                    {promptBlocking.map((c) => (
                      <ChangeRow
                        key={`blocking-prompt-${c.field}`}
                        label={c.field}
                        changeType={c.change_type}
                        onOpenDiff={diffOpener(c)}
                      />
                    ))}
                  </ChangeGroup>
                )}
                {settingBlocking.length > 0 && (
                  <ChangeGroup title="Agent settings">
                    {settingBlocking.map((c) => (
                      <ChangeRow
                        key={`blocking-setting-${c.field}`}
                        label={c.field}
                        changeType={c.change_type}
                        onOpenDiff={diffOpener(c)}
                      />
                    ))}
                  </ChangeGroup>
                )}
              </div>
            </>
          )}

          {files.length > 0 && (
            <>
              <Separator />
              <div className="space-y-2">
                <p className="text-xs font-medium">
                  Will be replaced by this pull
                </p>
                <p className="text-[11px] text-muted-foreground">
                  The workspace is replaced wholesale by either choice — keeping
                  your changes keeps the fields above, not these files. A
                  snapshot is saved first either way.
                </p>
                <ChangeGroup title="Workspace">
                  {files.map((c) => (
                    <ChangeRow
                      key={`file-${c.path}`}
                      label={c.path}
                      changeType={c.change_type}
                      onOpenDiff={() =>
                        setDiffTarget({
                          section: "file",
                          key: c.path,
                          label: c.path,
                        })
                      }
                    />
                  ))}
                </ChangeGroup>
              </div>
            </>
          )}

          <Separator />
          <p className="flex items-start gap-1.5 text-[11px] text-muted-foreground">
            <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-green-600 dark:text-green-500" />
            <span>
              Not touched: App Data, credentials, plugins and schedules.
            </span>
          </p>
        </div>

        {nothingBlocks ? (
          <p className="text-xs text-muted-foreground">
            Nothing in this agent's prompts or settings blocks the pull.
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            Click any change above to see its diff. Keeping your changes pulls
            everything else and leaves this agent with uncommitted changes on the
            fields above — commit them to push. Your current workspace is
            snapshotted first, whichever you choose.
          </p>
        )}

        <DialogFooter className="gap-2 sm:justify-between">
          {/* Discard sits where Cancel used to: the two resolutions are the only
              exits, and neither fires without its own confirmation. Closing the
              dialog (Esc / the X) is still the way out without pulling. */}
          {nothingBlocks ? (
            <span />
          ) : (
            <Button
              variant="outline"
              className="text-destructive hover:text-destructive"
              onClick={() => setPendingResolution("take_remote")}
              disabled={actionsDisabled}
            >
              Discard my changes and take remote
            </Button>
          )}
          <div className="flex flex-wrap items-center justify-end gap-2">
            {nothingBlocks ? (
              <Button
                onClick={() => setPendingResolution("plain")}
                disabled={actionsDisabled}
              >
                {isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
                ) : null}
                Pull
              </Button>
            ) : (
              <Button
                onClick={() => setPendingResolution("keep_local")}
                disabled={actionsDisabled}
              >
                {isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
                ) : null}
                Keep my changes
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>

      {/* Per-row diff — mounted inside the Dialog so closing it returns focus
          here rather than to the page behind. */}
      <GitDiffDialog
        agentId={agentId}
        target={diffTarget}
        onClose={() => setDiffTarget(null)}
      />

      {/* Confirmation for BOTH resolutions. Each pull is a workspace-replacing,
          environment-restarting operation, so neither gets a bare click. */}
      <AlertDialog
        open={pendingResolution !== null}
        onOpenChange={(o) => !o && setPendingResolution(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pendingResolution === "take_remote"
                ? "Discard your local changes?"
                : pendingResolution === "keep_local"
                  ? "Keep your local changes and pull?"
                  : "Pull from the remote?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pendingResolution === "take_remote" ? (
                <>
                  This replaces your local prompts and settings with the remote
                  version, and replaces the workspace. A backup snapshot of this
                  agent is saved first, so the work is recoverable by support if
                  you need it. The environment restarts.
                </>
              ) : pendingResolution === "keep_local" ? (
                <>
                  The prompts and settings listed as blocking stay as they are;
                  everything else comes from the remote, including the workspace
                  files. This agent will still have uncommitted changes
                  afterwards — commit them to push. A backup snapshot is saved
                  first and the environment restarts.
                </>
              ) : (
                <>
                  Nothing blocks this pull, but it still replaces the workspace
                  with the remote version and restarts the environment.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                // "plain" is the bodiless pull; the two named modes ride
                // `conflict_resolution`.
                onResolve(
                  pendingResolution === "plain"
                    ? undefined
                    : (pendingResolution as GitPullResolution),
                )
              }}
              disabled={isPending}
              className={
                pendingResolution === "take_remote"
                  ? "bg-destructive text-destructive-foreground hover:bg-destructive/90"
                  : undefined
              }
            >
              {isPending ? (
                <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
              ) : null}
              {pendingResolution === "take_remote"
                ? "Yes, discard my changes"
                : pendingResolution === "keep_local"
                  ? "Keep my changes and pull"
                  : "Pull"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Dialog>
  )
}
