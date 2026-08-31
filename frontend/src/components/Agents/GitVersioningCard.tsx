import { type ReactNode, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import {
  AlertTriangle,
  ArrowDownToLine,
  ArrowDownUp,
  ArrowUpFromLine,
  CheckCircle2,
  ExternalLink,
  Folder,
  GitBranch,
  GitCommitHorizontal,
  Loader2,
  type LucideIcon,
  RefreshCw,
  Unplug,
} from "lucide-react"

import { AgentGitService, type GitStatus } from "@/client"
import { getErrorMessage } from "@/utils"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import { Separator } from "@/components/ui/separator"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
import { DeployKeySelect } from "./DeployKeySelect"
import { ChangeGroup, ChangeRow } from "./GitChangeList"
import { GitDiffDialog, type GitDiffTarget } from "./GitDiffDialog"
import {
  type GitBlockingChange,
  GitPullConflictDialog,
  type GitPullResolution,
  localChangesBlocking,
} from "./GitPullConflictDialog"

const DEFAULT_CONNECT_MESSAGE = "Initial export from Cinna"
// Only the most recent commits are shown inline; full history lives on the
// remote host (linked via "View history" when a web URL can be generated).
const LATEST_COMMITS_COUNT = 3
// Throttle the git status reads. The dirty/status/commits checks do real work
// backend-side (the dirty/status reads snapshot + hash the workspace; commits
// clones), so treat results as fresh for 30s to avoid hammering the backend
// from per-card polling / refocus. Explicit user actions (connect / push /
// pull / the Refresh button) invalidate these queries directly.
const GIT_QUERY_STALE_TIME_MS = 30_000

// sync_direction → icon + hover title (replaces the plain-text direction label).
const SYNC_DIRECTION_META: Record<
  string,
  { icon: LucideIcon; title: string }
> = {
  bidirectional: {
    icon: ArrowDownUp,
    title: "Bidirectional sync (push & pull)",
  },
  pull: { icon: ArrowDownToLine, title: "Pull only" },
  push: { icon: ArrowUpFromLine, title: "Push only" },
}

function SyncDirectionIcon({ direction }: { direction?: string | null }) {
  const meta = direction ? SYNC_DIRECTION_META[direction] : undefined
  if (!meta) return null
  const Icon = meta.icon
  return (
    <span
      title={meta.title}
      aria-label={meta.title}
      className="inline-flex items-center text-muted-foreground"
    >
      <Icon className="h-3.5 w-3.5" />
    </span>
  )
}

// Normalize a pasted HTTP(S) repo URL into the SSH ("git@host:owner/repo.git")
// form git operations use with deploy keys. Mirrors the backend
// `convert_https_to_ssh_url`. SSH URLs and anything unrecognized pass through
// unchanged (the backend re-derives HTTPS for public/no-key clones).
function toGitSshUrl(rawUrl: string): string {
  const url = rawUrl.trim()
  if (!url || url.startsWith("git@")) return url
  const match = url.match(/^https?:\/\/([^/]+)\/(.+?)(?:\.git)?\/?$/i)
  if (!match) return url
  const [, host, path] = match
  return `git@${host}:${path}.git`
}

// Inline "code" style chip for the subdir / branch coordinates. An optional
// leading icon distinguishes a folder (subdir) from a branch (ref) at a glance.
function CodeChip({
  children,
  icon: Icon,
}: {
  children: ReactNode
  icon?: LucideIcon
}) {
  return (
    <code className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground">
      {Icon && <Icon className="h-3 w-3 text-muted-foreground" />}
      {children}
    </code>
  )
}

function errorStatus(error: unknown): number | undefined {
  return (error as { status?: number } | null)?.status
}

// Both connect and pull can return a *recoverable* 409 whose detail is a
// structured object carrying a machine-readable `code`, so the UI can offer the
// next step instead of a dead-end toast.
function isRecoverableConflict(error: unknown, code: string): boolean {
  if (errorStatus(error) !== 409) return false
  const detail = (error as { body?: { detail?: unknown } } | null)?.body?.detail
  return (
    typeof detail === "object" &&
    detail !== null &&
    (detail as { code?: string }).code === code
  )
}

// The connect endpoint returns detail.code === "existing_agent_folder" when the
// target subdir already holds an agent — recoverable by adopting it.
function isExistingFolderError(error: unknown): boolean {
  return isRecoverableConflict(error, "existing_agent_folder")
}

// The pull endpoint returns detail.code === "local_changes" when local state a
// pull would overwrite has drifted — recoverable via conflict_resolution.
function isLocalChangesError(error: unknown): boolean {
  return isRecoverableConflict(error, "local_changes")
}

interface GitVersioningCardProps {
  agentId: string
  agentName: string
  /**
   * Whether git versioning is connected, from the already-loaded agent payload.
   * Drives the toggle's initial state so it shows the real status immediately,
   * before this card's own git-source query (which fills the internals) resolves.
   */
  gitVersioningEnabled: boolean
}

/**
 * "Git Versioning" Integrations-tab card.
 *
 * Disabled by default (no AgentGitSource row). Toggling on reveals the connect
 * form (initial export push). Once connected it shows status, an update banner
 * with Pull, a dirty-gated "Commit Agent" push, commit history, and disconnect.
 */
export function GitVersioningCard({
  agentId,
  agentName,
  gitVersioningEnabled,
}: GitVersioningCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Connect form state.
  const [enableRequested, setEnableRequested] = useState(false)
  const [repoUrl, setRepoUrl] = useState("")
  const [subdir, setSubdir] = useState("")
  const [ref, setRef] = useState("main")
  const [syncDirection, setSyncDirection] = useState("bidirectional")
  const [sshKeyId, setSshKeyId] = useState<string | null>(null)
  const [connectMessage, setConnectMessage] = useState(DEFAULT_CONNECT_MESSAGE)

  // Commit dialog + disconnect confirm + adopt-existing-folder confirm.
  const [commitDialogOpen, setCommitDialogOpen] = useState(false)
  const [pushMessage, setPushMessage] = useState("")
  const [disconnectOpen, setDisconnectOpen] = useState(false)
  const [adoptOpen, setAdoptOpen] = useState(false)
  // Per-row diff opened from the commit preview (the pull-conflict dialog owns
  // its own instance, since it can be open independently of this one).
  const [commitDiffTarget, setCommitDiffTarget] =
    useState<GitDiffTarget | null>(null)

  // Pull-conflict dialog. `pullConflictBlocking` seeds it from a 409's
  // detail.blocking so the reactive path shows the fields immediately, before
  // the dialog's own status query resolves.
  const [pullConflictOpen, setPullConflictOpen] = useState(false)
  const [pullConflictBlocking, setPullConflictBlocking] = useState<
    GitBlockingChange[]
  >([])

  const resetConnectForm = () => {
    setEnableRequested(false)
    setRepoUrl("")
    setSubdir("")
    setRef("main")
    setSyncDirection("bidirectional")
    setSshKeyId(null)
    setConnectMessage(DEFAULT_CONNECT_MESSAGE)
  }

  // ---- Queries ----

  const {
    data: source,
    isLoading: isLoadingSource,
    error: sourceError,
  } = useQuery({
    queryKey: ["git-source", agentId],
    queryFn: () => AgentGitService.getGitSource({ agentId }),
    retry: false,
    staleTime: GIT_QUERY_STALE_TIME_MS,
  })

  const connected = !!source
  const noSource = !source && errorStatus(sourceError) === 404
  // True once the git-source query has settled (data or a definitive 404).
  const sourceResolved = connected || noSource
  // The toggle's checked state: authoritative once the query resolves, otherwise
  // the agent-payload flag — so the switch shows the real status from first paint
  // instead of flashing "off" then flipping on.
  const effectiveConnected = sourceResolved ? connected : gitVersioningEnabled

  const {
    data: dirty,
    isFetching: isDirtyFetching,
    isError: isDirtyError,
    error: dirtyError,
  } = useQuery({
    queryKey: ["git-dirty", agentId],
    queryFn: () => AgentGitService.getGitDirty({ agentId }),
    enabled: connected,
    // The dirty check can fail loud (503) when the last-synced baseline snapshot
    // was lost server-side and could not be rebuilt. Don't retry-loop that — surface
    // the failure straight away so the card shows it instead of the loading state.
    retry: false,
    staleTime: GIT_QUERY_STALE_TIME_MS,
  })

  // Freshness (the "remote has new commits → Pull" banner) is owned by the
  // explicit check-updates endpoint — the plain getGitSource read is remote-free
  // so it never blocks on / pins resources behind a slow remote. Strict endpoint:
  // a transient network/auth failure leaves updateStatus undefined (banner hidden)
  // rather than retrying in a loop.
  const { data: updateStatus } = useQuery({
    queryKey: ["git-check-updates", agentId],
    queryFn: () => AgentGitService.checkGitUpdates({ agentId }),
    enabled: connected,
    retry: false,
    staleTime: GIT_QUERY_STALE_TIME_MS,
  })

  const { data: commitsData, isLoading: isLoadingCommits } = useQuery({
    queryKey: ["git-commits", agentId, LATEST_COMMITS_COUNT],
    queryFn: () =>
      AgentGitService.listGitCommits({ agentId, limit: LATEST_COMMITS_COUNT }),
    enabled: connected,
    staleTime: GIT_QUERY_STALE_TIME_MS,
  })
  const commits = commitsData?.commits ?? []

  // Detailed commit preview — only fetched while the dialog is open (it does a
  // full workspace snapshot + per-file diff, heavier than the dirty poll).
  const { data: status, isLoading: isLoadingStatus } = useQuery({
    queryKey: ["git-status", agentId],
    queryFn: () => AgentGitService.getGitStatus({ agentId }),
    enabled: connected && commitDialogOpen,
    staleTime: GIT_QUERY_STALE_TIME_MS,
  })

  // ---- Mutations ----

  const connectMutation = useMutation({
    mutationFn: (adoptExisting: boolean) =>
      AgentGitService.connectGitSource({
        agentId,
        requestBody: {
          repo_url: repoUrl.trim(),
          subdir: subdir.trim() || null,
          ref: ref.trim() || "main",
          ssh_key_id: sshKeyId,
          sync_direction: syncDirection,
          commit_message: connectMessage.trim() || DEFAULT_CONNECT_MESSAGE,
          adopt_existing: adoptExisting,
        },
      }),
    onSuccess: () => {
      showSuccessToast("Git versioning enabled")
      setAdoptOpen(false)
      resetConnectForm()
      queryClient.invalidateQueries({ queryKey: ["git-source", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-commits", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-dirty", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-status", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-check-updates", agentId] })
      // Refresh the agent payload so its git_versioning_enabled flag (the
      // toggle's initial state) reflects the new connection.
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
    onError: (error) => {
      // Folder already holds an agent → offer to adopt it instead of failing.
      if (isExistingFolderError(error)) {
        setAdoptOpen(true)
        return
      }
      setAdoptOpen(false)
      showErrorToast(getErrorMessage(error, "Failed to connect git source"))
    },
  })

  const pushMutation = useMutation({
    mutationFn: (message: string) =>
      AgentGitService.pushGitSource({
        agentId,
        requestBody: { commit_message: message },
      }),
    onSuccess: () => {
      showSuccessToast("Agent committed")
      setCommitDialogOpen(false)
      setPushMessage("")
      queryClient.invalidateQueries({ queryKey: ["git-source", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-commits", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-dirty", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-status", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-check-updates", agentId] })
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Failed to commit agent")),
  })

  const pullMutation = useMutation({
    mutationFn: (resolution?: GitPullResolution) =>
      AgentGitService.pullGitSource({
        agentId,
        // No resolution ⇒ no body at all, preserving the fail-loud semantics of
        // the plain pull (and of the GitOps webhook path it shares).
        requestBody: resolution ? { conflict_resolution: resolution } : undefined,
      }),
    onSuccess: (_data, resolution) => {
      showSuccessToast(
        resolution === "keep_local"
          ? // keep_local leaves the install dirty on the preserved fields BY
            // DESIGN (pull, then commit on top). Say so, or it reads as a
            // failed pull the moment the card re-renders as "dirty".
            //
            // Names prompts/settings rather than "your local changes": the
            // workspace is replaced on BOTH resolutions, so the broader wording
            // would promise a locally edited file survived when it did not.
            // The workspace snapshot is what makes that recoverable.
            "Pulled. Your prompts and settings were kept — commit them to " +
              "push. The previous workspace was snapshotted first."
          : resolution === "take_remote"
            ? "Pulled. Local changes were discarded — the previous state was " +
              "snapshotted first."
            : "Pulled latest changes",
      )
      setPullConflictOpen(false)
      setPullConflictBlocking([])
      queryClient.invalidateQueries({ queryKey: ["git-source", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-commits", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-dirty", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-status", agentId] })
      queryClient.invalidateQueries({ queryKey: ["git-check-updates", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
    onError: (error) => {
      // A race (someone committed between the status read and the pull) can
      // still produce the 409 — open the same dialog, seeded from its payload,
      // instead of a toast the user cannot act on.
      if (isLocalChangesError(error)) {
        setPullConflictBlocking(localChangesBlocking(error))
        setPullConflictOpen(true)
        // The cached status predates whatever caused this 409, and the dialog
        // may already be open (so opening it again is a no-op that would not
        // refetch anything). Drop it so the "will be replaced" file list is
        // recomputed; the seeded blocking list covers the gap meanwhile.
        queryClient.invalidateQueries({ queryKey: ["git-status", agentId] })
        return
      }
      showErrorToast(getErrorMessage(error, "Failed to pull changes"))
    },
  })

  const disconnectMutation = useMutation({
    mutationFn: () => AgentGitService.disconnectGitSource({ agentId }),
    onSuccess: () => {
      showSuccessToast("Git versioning disabled")
      setDisconnectOpen(false)
      resetConnectForm()
      // The source is gone (GET /git now 404s). invalidate alone keeps the last
      // successful data on the errored refetch, so the card would stay in the
      // connected state — remove the cached entries to reset it to disabled.
      queryClient.removeQueries({ queryKey: ["git-source", agentId] })
      queryClient.removeQueries({ queryKey: ["git-dirty", agentId] })
      queryClient.removeQueries({ queryKey: ["git-status", agentId] })
      queryClient.removeQueries({ queryKey: ["git-commits", agentId] })
      queryClient.removeQueries({ queryKey: ["git-check-updates", agentId] })
      // Refresh the agent payload so its git_versioning_enabled flag clears.
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Failed to disconnect git source")),
  })

  // ---- Derived UI state ----

  // An advanced remote on a dirty agent is the deadlock case: a plain Pull is
  // guaranteed to 409 (and the remedy it used to suggest — push first — is
  // itself blocked by the remote advance). Send the user to the dialog instead.
  // `dirty` is the broad indicator, so this can over-trigger (a workspace-only
  // change does not block); the dialog then shows an empty "Blocks the pull"
  // section and "Keep my changes" behaves exactly like a plain pull.
  const pullNeedsReview = !!updateStatus?.update_available && !!dirty?.dirty

  const showConnectForm = enableRequested && !connected
  const toggleChecked = effectiveConnected || enableRequested
  const toggleBusy = connectMutation.isPending || disconnectMutation.isPending
  // Internals (repo coordinates, commits, dirty) are still loading when the
  // toggle reads enabled but the git-source query hasn't resolved yet.
  const showInternalsSpinner =
    !sourceResolved && !showConnectForm && (gitVersioningEnabled || isLoadingSource)

  const handleToggle = (checked: boolean) => {
    if (checked) {
      if (!effectiveConnected) setEnableRequested(true)
    } else if (effectiveConnected) {
      setDisconnectOpen(true)
    } else {
      setEnableRequested(false)
    }
  }

  const handleConnect = () => {
    if (!repoUrl.trim()) return
    connectMutation.mutate(false)
  }

  const handleOpenCommitDialog = () => {
    setPushMessage("")
    setCommitDialogOpen(true)
  }

  // Re-check what would be committed (dirty gate + update banner + commit list).
  const handleRefresh = () => {
    queryClient.invalidateQueries({ queryKey: ["git-dirty", agentId] })
    queryClient.invalidateQueries({ queryKey: ["git-status", agentId] })
    queryClient.invalidateQueries({ queryKey: ["git-source", agentId] })
    queryClient.invalidateQueries({ queryKey: ["git-commits", agentId] })
    queryClient.invalidateQueries({ queryKey: ["git-check-updates", agentId] })
  }

  const handlePush = () => {
    const message = pushMessage.trim()
    if (!message) return
    pushMutation.mutate(message)
  }

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <GitBranch className="h-5 w-5" />
                Git Versioning
              </CardTitle>
              <CardDescription>
                Version this agent's workspace in an external git repository.
              </CardDescription>
            </div>
            <Switch
              checked={toggleChecked}
              onCheckedChange={handleToggle}
              disabled={toggleBusy}
              aria-label="Enable git versioning"
              className="ml-4 mt-1"
            />
          </div>
        </CardHeader>
        <CardContent>
          {showInternalsSpinner ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading git versioning…
            </div>
          ) : showConnectForm ? (
            <ConnectForm
              repoUrl={repoUrl}
              setRepoUrl={setRepoUrl}
              subdir={subdir}
              setSubdir={setSubdir}
              gitRef={ref}
              setGitRef={setRef}
              syncDirection={syncDirection}
              setSyncDirection={setSyncDirection}
              sshKeyId={sshKeyId}
              setSshKeyId={setSshKeyId}
              connectMessage={connectMessage}
              setConnectMessage={setConnectMessage}
              pending={connectMutation.isPending}
              onConnect={handleConnect}
            />
          ) : connected && source ? (
            <div className="space-y-4">
              {/* Source coordinates + status */}
              <div className="space-y-1.5">
                <div className="flex items-center gap-2 flex-wrap">
                  {source.web_tree_url ? (
                    <a
                      href={source.web_tree_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-sm break-all text-primary hover:underline"
                    >
                      {source.repo_url}
                    </a>
                  ) : (
                    <span className="font-mono text-sm break-all">
                      {source.repo_url}
                    </span>
                  )}
                  {source.status === "error" ? (
                    <Badge variant="destructive">error</Badge>
                  ) : source.status === "connected" ? (
                    <span
                      title="Connected"
                      aria-label="Connected"
                      className="inline-flex items-center text-green-600 dark:text-green-500"
                    >
                      <CheckCircle2 className="h-4 w-4" />
                    </span>
                  ) : (
                    <Badge variant="secondary">{source.status}</Badge>
                  )}
                </div>
                <div className="flex items-center gap-1.5 flex-wrap">
                  {source.subdir && (
                    <CodeChip icon={Folder}>{source.subdir}</CodeChip>
                  )}
                  <CodeChip icon={GitBranch}>{source.ref ?? "main"}</CodeChip>
                  <SyncDirectionIcon direction={source.sync_direction} />
                </div>
                {source.status === "error" && source.last_error && (
                  <p className="text-xs text-destructive">{source.last_error}</p>
                )}
              </div>

              {/* Update banner — gated on the strict check-updates result (the
                  plain source read is remote-free and never sets this). When the
                  agent is also dirty, a plain Pull is guaranteed to 409, so the
                  banner pre-empts it and routes to the conflict dialog. */}
              {updateStatus?.update_available && (
                <div className="flex items-center justify-between gap-3 rounded-md border border-amber-200 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950/40">
                  <p className="text-xs text-amber-800 dark:text-amber-200">
                    {pullNeedsReview
                      ? "The remote has new commits, but this agent has local changes."
                      : "The remote has new commits. Pull to update this agent."}
                  </p>
                  <Button
                    size="sm"
                    variant="outline"
                    className="shrink-0"
                    onClick={() => {
                      if (pullNeedsReview) {
                        setPullConflictBlocking([])
                        setPullConflictOpen(true)
                      } else {
                        pullMutation.mutate(undefined)
                      }
                    }}
                    disabled={pullMutation.isPending}
                  >
                    {pullMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
                    ) : (
                      <ArrowDownToLine className="h-4 w-4 mr-1.5" />
                    )}
                    {pullNeedsReview ? "Review & pull" : "Pull"}
                  </Button>
                </div>
              )}

              <Separator />

              {/* Latest commits */}
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm font-medium">Latest commits</p>
                  {source.web_history_url && (
                    <a
                      href={source.web_history_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                    >
                      View history
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                </div>
                {isLoadingCommits ? (
                  <p className="text-sm text-muted-foreground">Loading…</p>
                ) : commits.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No commits yet.</p>
                ) : (
                  <ul className="divide-y divide-border">
                    {commits.slice(0, LATEST_COMMITS_COUNT).map((commit) => (
                      <li
                        key={commit.sha}
                        className="flex items-start gap-3 py-2"
                      >
                        <GitCommitHorizontal className="h-4 w-4 mt-0.5 text-muted-foreground shrink-0" />
                        <div className="min-w-0">
                          <p className="text-sm truncate">{commit.message}</p>
                          <p className="text-xs text-muted-foreground">
                            {commit.commit_url ? (
                              <a
                                href={commit.commit_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="font-mono text-primary hover:underline"
                              >
                                {commit.short_sha}
                              </a>
                            ) : (
                              <span className="font-mono">
                                {commit.short_sha}
                              </span>
                            )}{" "}
                            · {commit.author_name} ·{" "}
                            {formatDistanceToNow(new Date(commit.date), {
                              addSuffix: true,
                            })}
                          </p>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ) : sourceError && !noSource ? (
            <p className="text-sm text-destructive">
              {getErrorMessage(
                sourceError,
                "Failed to load git versioning status.",
              )}
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">
              Connect an external git repository to keep durable, versioned
              snapshots of this agent's workspace. Enable to get started.
            </p>
          )}
        </CardContent>
        {connected && source && (
          <CardFooter className="flex items-center justify-between gap-2">
            {/* Left: Commit Agent + status reason + refresh. Push requires a
                live env and a dirty workspace, with an accurate reason. */}
            <div className="flex items-center gap-2 min-w-0">
              <Button
                size="sm"
                onClick={handleOpenCommitDialog}
                disabled={!dirty?.dirty || isDirtyError || pushMutation.isPending}
              >
                Commit Agent
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={handleRefresh}
                disabled={isDirtyFetching}
                aria-label="Refresh status"
                title="Re-check changes to commit"
              >
                <RefreshCw
                  className={`h-4 w-4 ${isDirtyFetching ? "animate-spin" : ""}`}
                />
              </Button>
              {isDirtyError ? (
                <span
                  className="flex items-center gap-1 text-xs text-destructive truncate"
                  title={getErrorMessage(
                    dirtyError,
                    "The last-synced baseline could not be verified.",
                  )}
                >
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">
                    Baseline check failed — re-sync (pull or commit) to rebuild it
                  </span>
                </span>
              ) : dirty === undefined ? (
                <span className="text-xs text-muted-foreground truncate">
                  Checking for changes…
                </span>
              ) : dirty.has_env === false ? (
                <span className="text-xs text-muted-foreground truncate">
                  Start the environment to commit
                </span>
              ) : !dirty.dirty ? (
                <span className="text-xs text-muted-foreground truncate">
                  No local changes
                </span>
              ) : null}
            </div>

            {/* Right: Disconnect (icon-only). */}
            <Button
              size="icon-sm"
              variant="ghost"
              className="shrink-0 text-muted-foreground hover:text-destructive"
              onClick={() => setDisconnectOpen(true)}
              aria-label="Disconnect git versioning"
              title="Disconnect git versioning"
            >
              <Unplug className="h-4 w-4" />
            </Button>
          </CardFooter>
        )}
      </Card>

      {/* Commit message dialog */}
      <Dialog open={commitDialogOpen} onOpenChange={setCommitDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Commit Agent</DialogTitle>
            <DialogDescription>
              Capture the current workspace of {agentName} and push it to the
              remote as a new commit.
            </DialogDescription>
          </DialogHeader>

          {/* Changes to be committed — git status style preview */}
          <CommitPreview
            status={status}
            isLoading={isLoadingStatus}
            onOpenDiff={setCommitDiffTarget}
          />
          <GitDiffDialog
            agentId={agentId}
            target={commitDiffTarget}
            onClose={() => setCommitDiffTarget(null)}
          />

          <div className="space-y-2">
            <Label htmlFor="commit-message">Commit message</Label>
            <Input
              id="commit-message"
              placeholder="e.g., Update prompts and tools"
              value={pushMessage}
              onChange={(e) => setPushMessage(e.target.value)}
              autoFocus
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCommitDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button
              onClick={handlePush}
              disabled={!pushMessage.trim() || pushMutation.isPending}
            >
              {pushMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin mr-1.5" />
              ) : null}
              Commit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Pull conflict — what blocks, what gets replaced, and the two resolutions */}
      <GitPullConflictDialog
        open={pullConflictOpen}
        onOpenChange={(open) => {
          setPullConflictOpen(open)
          if (!open) setPullConflictBlocking([])
        }}
        agentId={agentId}
        agentName={agentName}
        remoteCommit={updateStatus?.remote_commit}
        commits={commits}
        seededBlocking={pullConflictBlocking}
        onResolve={(resolution) => pullMutation.mutate(resolution)}
        isPending={pullMutation.isPending}
      />

      {/* Disconnect confirm */}
      <AlertDialog open={disconnectOpen} onOpenChange={setDisconnectOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect git versioning?</AlertDialogTitle>
            <AlertDialogDescription>
              This removes the link between this agent and the git repository.
              The external repository and its history are left untouched — you
              can reconnect later.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                disconnectMutation.mutate()
              }}
              disabled={disconnectMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {disconnectMutation.isPending ? "Disconnecting…" : "Disconnect"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Adopt existing remote folder confirm */}
      <AlertDialog open={adoptOpen} onOpenChange={setAdoptOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Folder already exists</AlertDialogTitle>
            <AlertDialogDescription>
              This repository folder already holds an agent. Do you wish to
              establish a connection to this exact folder? Nothing is overwritten
              — we link to it and re-check the differences between your local
              workspace and the remote, which you can then commit or pull.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={connectMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                connectMutation.mutate(true)
              }}
              disabled={connectMutation.isPending}
            >
              {connectMutation.isPending ? "Connecting…" : "Connect to folder"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

// ---------------------------------------------------------------------------
// CommitPreview — "git status" style list of changes to be committed
//
// `CHANGE_META` / `ChangeRow` / `ChangeGroup` live in ./GitChangeList so this
// preview and the pull-conflict dialog render the same change list identically.
// ---------------------------------------------------------------------------

function CommitPreview({
  status,
  isLoading,
  onOpenDiff,
}: {
  status: GitStatus | undefined
  isLoading: boolean
  onOpenDiff: (target: GitDiffTarget) => void
}) {
  if (isLoading || !status) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Computing changes…
      </div>
    )
  }

  const prompts = status.prompt_changes ?? []
  const settings = status.setting_changes ?? []
  const files = status.file_changes ?? []

  if (prompts.length === 0 && settings.length === 0 && files.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No changes detected to commit.
      </p>
    )
  }

  return (
    <div className="space-y-3 rounded-md border bg-muted/40 p-3 max-h-60 overflow-y-auto">
      <p className="text-xs font-medium text-muted-foreground">
        Changes to be committed
      </p>
      {prompts.length > 0 && (
        <ChangeGroup title="Prompts">
          {prompts.map((c) => {
            // Bind to a local so the closure captures a narrowed string; the
            // generated types make `key` optional (it has a server default).
            const diffKey = c.key
            return (
              <ChangeRow
                key={`prompt-${c.field}`}
                label={c.field}
                changeType={c.change_type}
                onOpenDiff={
                  diffKey
                    ? () =>
                        onOpenDiff({
                          section: c.section || "prompt",
                          key: diffKey,
                          label: c.field,
                        })
                    : undefined
                }
              />
            )
          })}
        </ChangeGroup>
      )}
      {settings.length > 0 && (
        <ChangeGroup title="Agent settings">
          {settings.map((c) => {
            // `section` disambiguates which registry the key belongs to;
            // without both, the endpoint cannot resolve the live side.
            const diffKey = c.key
            const diffSection = c.section
            return (
              <ChangeRow
                key={`setting-${c.field}`}
                label={c.field}
                changeType={c.change_type}
                onOpenDiff={
                  diffKey && diffSection
                    ? () =>
                        onOpenDiff({
                          section: diffSection,
                          key: diffKey,
                          label: c.field,
                        })
                    : undefined
                }
              />
            )
          })}
        </ChangeGroup>
      )}
      {files.length > 0 && (
        <ChangeGroup title="Workspace">
          {files.map((c) => (
            <ChangeRow
              key={`file-${c.path}`}
              label={c.path}
              changeType={c.change_type}
              onOpenDiff={() =>
                onOpenDiff({
                  section: "file",
                  key: c.path,
                  label: c.path,
                })
              }
            />
          ))}
        </ChangeGroup>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ConnectForm
// ---------------------------------------------------------------------------

interface ConnectFormProps {
  repoUrl: string
  setRepoUrl: (v: string) => void
  subdir: string
  setSubdir: (v: string) => void
  gitRef: string
  setGitRef: (v: string) => void
  syncDirection: string
  setSyncDirection: (v: string) => void
  sshKeyId: string | null
  setSshKeyId: (v: string | null) => void
  connectMessage: string
  setConnectMessage: (v: string) => void
  pending: boolean
  onConnect: () => void
}

function ConnectForm({
  repoUrl,
  setRepoUrl,
  subdir,
  setSubdir,
  gitRef,
  setGitRef,
  syncDirection,
  setSyncDirection,
  sshKeyId,
  setSshKeyId,
  connectMessage,
  setConnectMessage,
  pending,
  onConnect,
}: ConnectFormProps) {
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="git-repo-url">Repository URL</Label>
        <Input
          id="git-repo-url"
          placeholder="git@github.com:org/repo.git"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          onBlur={(e) => setRepoUrl(toGitSshUrl(e.target.value))}
          className="font-mono text-sm"
        />
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label htmlFor="git-ref">Branch / ref</Label>
          <Input
            id="git-ref"
            placeholder="main"
            value={gitRef}
            onChange={(e) => setGitRef(e.target.value)}
            className="font-mono text-sm"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="git-subdir">Subdirectory (optional)</Label>
          <Input
            id="git-subdir"
            placeholder="agents/my-agent"
            value={subdir}
            onChange={(e) => setSubdir(e.target.value)}
            className="font-mono text-sm"
          />
        </div>
      </div>
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-0.5">
          <Label>Sync direction</Label>
          <p className="text-xs text-muted-foreground">
            Connecting performs an initial export push, so choose Bidirectional
            or Push only.
          </p>
        </div>
        <Select value={syncDirection} onValueChange={setSyncDirection}>
          <SelectTrigger className="w-[200px] shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="bidirectional">Bidirectional</SelectItem>
            <SelectItem value="push">Push only</SelectItem>
            <SelectItem value="pull">Pull only</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <DeployKeySelect value={sshKeyId} onChange={setSshKeyId} />

      <div className="space-y-2">
        <Label htmlFor="git-connect-message">Initial commit message</Label>
        <Input
          id="git-connect-message"
          value={connectMessage}
          onChange={(e) => setConnectMessage(e.target.value)}
        />
      </div>

      <div className="flex justify-end">
        <Button onClick={onConnect} disabled={!repoUrl.trim() || pending}>
          {pending ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
          Connect
        </Button>
      </div>
    </div>
  )
}
