import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  CheckCircle2,
  EllipsisVertical,
  Pencil,
  Trash,
  UsersRound,
} from "lucide-react"
import { useState } from "react"

import {
  type ManagedAICredentialApplyResult,
  type ManagedAICredentialPublic,
  AdminLlmProvidersService,
} from "@/client"
import { ApiError } from "@/client/core/ApiError"
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { userRoleLabel } from "@/utils/userRoles"
import { ManagedCredentialDialog } from "./ManagedCredentialDialog"
import {
  getProviderTypeLabel,
  knownSdkModes,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  sdkModeLabel,
} from "./providerTypes"

interface LlmProviderActionsMenuProps {
  record: ManagedAICredentialPublic
}

// One blocked member from a 409 delete response.
interface BlockedMember {
  user_id: string
  reason: string
  impact?: unknown
}

// Extract the blocked-members list from a 409 ApiError body
// ({ detail: { message, blocked: [...] } }).
function blockedFromError(error: unknown): BlockedMember[] | null {
  if (error instanceof ApiError && error.status === 409) {
    const detail = (error.body as { detail?: unknown } | undefined)?.detail
    if (detail && typeof detail === "object" && "blocked" in detail) {
      const blocked = (detail as { blocked?: unknown }).blocked
      if (Array.isArray(blocked)) return blocked as BlockedMember[]
    }
  }
  return null
}

// Everything a mutation on this menu can move. Apply-to-existing repoints
// other people's defaults, delete removes their children, "set default for
// all" promotes them — and when `admin` is one of the record's
// auto-provision roles the acting superuser is a candidate like anyone else,
// so the stale rows include their own. Invalidating only the admin list left
// them looking at a default in Settings that the server had already changed.
const AFFECTED_QUERY_KEYS = [
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  ["currentUser"],
  ["aiCredentialsList"],
  ["aiCredentialsStatus"],
  ["resolveDefaultCredential"],
] as const

/** Plain-English list, e.g. "Agent User and Agent Developer". */
function joinLabels(labels: string[]): string {
  if (labels.length === 0) return ""
  if (labels.length === 1) return labels[0]
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`
}

export function LlmProviderActionsMenu({ record }: LlmProviderActionsMenuProps) {
  const [isEditOpen, setIsEditOpen] = useState(false)
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)
  const [isApplyOpen, setIsApplyOpen] = useState(false)
  // Populated when a non-forced delete is blocked by bundle usage (409); drives
  // the "force delete" confirmation copy.
  const [blocked, setBlocked] = useState<BlockedMember[] | null>(null)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Fire-and-forget on purpose, and the same way at every call site: these
  // mutations all close their dialog on success, so holding `isPending`
  // through a refetch would only delay the button going back to normal — and
  // on the delete path it would delay the 409 "force delete" affordance the
  // admin is waiting for. Unlike the roles matrix, nothing here computes its
  // next request from a cached row, so there is no correctness hazard in the
  // gap.
  const invalidate = () => {
    for (const queryKey of AFFECTED_QUERY_KEYS) {
      void queryClient.invalidateQueries({ queryKey })
    }
  }

  const memberLabelById = new Map(
    (record.members ?? []).map((m) => [
      m.user_id,
      m.full_name ? `${m.full_name} <${m.email}>` : m.email,
    ]),
  )
  const labelFor = (userId: string) => memberLabelById.get(userId) ?? userId

  const setDefaultMutation = useMutation({
    mutationFn: () =>
      AdminLlmProvidersService.setManagedAiCredentialDefault({
        managedCredentialId: record.id,
      }),
    onSuccess: () => {
      showSuccessToast("Set as the default credential for all members.")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => invalidate(),
  })

  // ── Apply to existing users ──────────────────────────────────────────
  // Two mutations rather than one with a `dryRun` flag: the preview has to
  // stay on screen while the real run is in flight, so its result cannot be
  // the same piece of state the commit overwrites.
  const previewMutation = useMutation<ManagedAICredentialApplyResult>({
    mutationFn: () =>
      AdminLlmProvidersService.applyManagedAiCredentialToExisting({
        managedCredentialId: record.id,
        dryRun: true,
      }),
  })

  const applyMutation = useMutation<ManagedAICredentialApplyResult, ApiError>({
    mutationFn: () =>
      AdminLlmProvidersService.applyManagedAiCredentialToExisting({
        managedCredentialId: record.id,
        dryRun: false,
      }),
    onSuccess: (result) => {
      const added = result.added ?? []
      const skipped = result.skipped ?? []
      // A skipped user is by definition not a member, so the record's member
      // list cannot name them. The preview can: it listed every candidate with
      // their email before any of this was attempted.
      const labelById = new Map(memberLabelById)
      for (const candidate of previewMutation.data?.candidates ?? []) {
        labelById.set(
          candidate.user_id,
          candidate.full_name
            ? `${candidate.full_name} <${candidate.email}>`
            : candidate.email,
        )
      }
      // One toast per skip, by name: a summary count would tell the admin that
      // somebody was left out without telling them who to go and fix.
      for (const skip of skipped) {
        showErrorToast(
          `${labelById.get(skip.user_id) ?? skip.user_id} skipped (${skip.reason}).`,
        )
      }
      // The preview is a snapshot; nothing locks the candidate set between the
      // dry run and this commit, and a signup in between is exactly the kind of
      // thing this feature exists alongside. Say so when the number moved,
      // rather than let the admin notice the toast disagreeing with the button
      // they just pressed.
      const drifted = result.candidate_count !== candidateCount
      if (added.length === 0 && skipped.length === 0) {
        showSuccessToast("Nobody was missing this credential — nothing changed.")
      } else if (added.length > 0) {
        showSuccessToast(
          drifted
            ? `Granted to ${added.length} ${added.length === 1 ? "account" : "accounts"} — ${candidateCount} were previewed, the set changed in between.`
            : `Granted to ${added.length} ${added.length === 1 ? "account" : "accounts"}.`,
        )
      }
      setIsApplyOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => invalidate(),
  })

  const openApplyDialog = () => {
    previewMutation.reset()
    applyMutation.reset()
    setIsApplyOpen(true)
    // Always. The table row this menu hangs off is up to 30s stale, and the
    // whole point of this feature is that the roles and the membership move on
    // their own — deciding from the row that there is nothing to ask about is
    // how the dialog ends up telling an admin a credential auto-provisions for
    // nobody while the server says otherwise.
    previewMutation.mutate()
  }

  const preview = previewMutation.data
  // Every number and every name in the dialog comes from one source: the dry
  // run's own projection while it is loaded, the stale row only until then.
  // Mixing the two produced sentences that contradicted their own counts.
  const previewRecord = preview?.record ?? record
  const autoRoles = previewRecord.auto_provision_roles ?? []
  // Every default slot this record takes over, named the way the admin reads
  // them. Both flags are counted by `_count_default_overwrites` and both have
  // to appear here: they are independent, and a dialog that described only the
  // SDK-defaults axis said *nothing whatsoever* about a record whose only
  // default-writing flag is `set_as_default` — the configuration that silently
  // demoted every candidate's own key while previewing as free.
  // Filtered to the real slots exactly as the backend filters them, so the
  // modes the copy names are the modes that were counted.
  const wiredModes = previewRecord.set_user_sdk_defaults
    ? knownSdkModes(previewRecord.sdk_default_modes)
    : []
  const claimedSlots = [
    ...wiredModes.map(
      (mode) => `the ${sdkModeLabel(mode).toLowerCase()} default`,
    ),
    ...(previewRecord.set_as_default
      ? [`the default ${getProviderTypeLabel(previewRecord.type)} credential`]
      : []),
  ]
  const claimedSlotsPhrase = joinLabels(claimedSlots)

  const candidateCount = preview?.candidate_count ?? 0
  const overwriteCount = preview?.defaults_overwrite_count ?? 0
  const canApply = autoRoles.length > 0 && !!preview && candidateCount > 0

  const deleteMutation = useMutation({
    mutationFn: (force: boolean) =>
      AdminLlmProvidersService.deleteManagedAiCredential({
        managedCredentialId: record.id,
        force,
      }),
    onSuccess: () => {
      showSuccessToast("Managed credential deleted.")
      setBlocked(null)
      setIsDeleteOpen(false)
    },
    onError: (error) => {
      const blockedMembers = blockedFromError(error)
      if (blockedMembers) {
        // Surface the Tier-2 block: keep the dialog open and switch it into the
        // "force delete" confirmation (mirrors the AI-credential Tier-2 flow).
        setBlocked(blockedMembers)
        showErrorToast(
          "One or more members are in use by a published bundle. Review below before forcing.",
        )
        return
      }
      handleError.call(showErrorToast, error as ApiError)
    },
    onSettled: () => invalidate(),
  })

  const isBlocked = (blocked?.length ?? 0) > 0

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0">
            <EllipsisVertical className="h-4 w-4" />
            <span className="sr-only">Open menu</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => setIsEditOpen(true)}>
            <Pencil className="mr-2 h-4 w-4" />
            Edit
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => setDefaultMutation.mutate()}
            disabled={setDefaultMutation.isPending}
          >
            <CheckCircle2 className="mr-2 h-4 w-4" />
            Set default for all
          </DropdownMenuItem>
          <DropdownMenuItem onClick={openApplyDialog}>
            <UsersRound className="mr-2 h-4 w-4" />
            Apply to existing users
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => {
              setBlocked(null)
              setIsDeleteOpen(true)
            }}
            className="text-destructive focus:text-destructive"
          >
            <Trash className="mr-2 h-4 w-4" />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Unified edit dialog */}
      <ManagedCredentialDialog
        mode="edit"
        record={record}
        open={isEditOpen}
        onOpenChange={setIsEditOpen}
      />

      {/* Apply to existing users — previews with a dry run, then commits */}
      <AlertDialog open={isApplyOpen} onOpenChange={setIsApplyOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Apply to existing users</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                {/* Loading and error come first. Until the dry run answers,
                    `previewRecord` is still the stale table row, and deciding
                    from it that this credential auto-provisions for nobody
                    would flash a wrong verdict over a request in flight. */}
                {previewMutation.isPending ? (
                  <p>Working out who would receive this credential…</p>
                ) : previewMutation.isError ? (
                  <div className="space-y-2">
                    <p>Couldn't work out who would receive this credential.</p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => previewMutation.mutate()}
                    >
                      Retry
                    </Button>
                  </div>
                ) : autoRoles.length === 0 ? (
                  <p>
                    "{previewRecord.name}" isn't set to auto-provision for any
                    role, so there is nobody to apply it to. Pick roles under
                    Auto-provision in Edit first.
                  </p>
                ) : candidateCount === 0 ? (
                  // A zero means "nobody is missing it" — which covers both
                  // "everyone already has it" and "no account holds these
                  // roles at all". The response cannot tell them apart, so the
                  // sentence must not pick one.
                  <p>
                    No active{" "}
                    {joinLabels(autoRoles.map(userRoleLabel))} account is
                    missing "{previewRecord.name}" right now — nothing to
                    apply.
                  </p>
                ) : (
                  <>
                    <p>
                      <strong>{candidateCount}</strong>{" "}
                      {candidateCount === 1 ? "account" : "accounts"} (
                      {joinLabels(autoRoles.map(userRoleLabel))}) will receive
                      "{previewRecord.name}".
                    </p>
                    {/* The count that costs something. The spec's preview
                        quotes only the first number, but replacing a default
                        somebody chose is the half an admin would want to know
                        about before confirming, not after. */}
                    {/* The `claimedSlots.length === 0` arm is not dead code
                        for a hypothetical future: it is the guarantee that the
                        dialog can never go silent about a cost the server has
                        already counted. A number it cannot narrate still gets
                        said. */}
                    {overwriteCount > 0 ? (
                      <p>
                        <strong>{overwriteCount}</strong> of them already{" "}
                        {overwriteCount === 1 ? "has" : "have"} a default this
                        grant takes away
                        {claimedSlots.length > 0
                          ? `. "${previewRecord.name}" becomes ${claimedSlotsPhrase} for every account here`
                          : ""}
                        {wiredModes.length > 0
                          ? ", and the model each of those modes was pinned to is reset."
                          : "."}
                      </p>
                    ) : (
                      claimedSlots.length > 0 && (
                        // Says what was actually measured. The count asks
                        // whether a credential or a model is pinned to each
                        // slot; it does not ask about `default_sdk_<mode>`,
                        // which is never NULL, so "nothing changes" would be
                        // an overclaim — an OpenAI record claiming the
                        // conversation slot does move everyone's engine.
                        <p>
                          None of them has picked a credential or a model for{" "}
                          {claimedSlotsPhrase} yet, so "{previewRecord.name}"
                          fills {claimedSlots.length === 1 ? "it" : "those"} in
                          rather than replacing a choice.
                        </p>
                      )
                    )}
                    <p>
                      Add-only — nobody loses a credential, and current members
                      are left alone.
                    </p>
                  </>
                )}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={applyMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                applyMutation.mutate()
              }}
              disabled={!canApply || applyMutation.isPending}
            >
              {applyMutation.isPending
                ? "Applying..."
                : canApply
                  ? `Grant to ${candidateCount} ${candidateCount === 1 ? "account" : "accounts"}`
                  : "Grant"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete confirm — escalates to a force confirmation on a 409 block */}
      <AlertDialog
        open={isDeleteOpen}
        onOpenChange={(open) => {
          setIsDeleteOpen(open)
          if (!open) setBlocked(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {isBlocked ? "Members in use by a bundle" : "Delete Managed Credential"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {isBlocked ? (
                <>
                  Some members couldn't be removed because their credential is in use
                  by a published bundle. Forcing the delete will degrade those bundles
                  back to "user provides". This action cannot be undone.
                </>
              ) : (
                <>
                  Delete "{record.name}" and every member's credential
                  {record.member_count
                    ? ` (${record.member_count} member${record.member_count === 1 ? "" : "s"})`
                    : ""}
                  ? This action cannot be undone.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>

          {isBlocked && (
            <ul className="space-y-1 text-sm">
              {blocked!.map((b) => (
                <li
                  key={b.user_id}
                  className="rounded-md border bg-muted/30 px-3 py-1.5 text-muted-foreground"
                >
                  {labelFor(b.user_id)}
                </li>
              ))}
            </ul>
          )}

          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                deleteMutation.mutate(isBlocked)
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending
                ? "Deleting..."
                : isBlocked
                  ? "Force delete & degrade bundles"
                  : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
