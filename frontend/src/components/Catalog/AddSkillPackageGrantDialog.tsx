import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { SkillsService } from "@/client"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { skillCatalogFailure } from "@/utils/skillCatalog"

interface AddSkillPackageGrantDialogProps {
  packageId: string
  packageName: string
  /** Everyone who already has access — not offered again by the picker. */
  existingUserIds: string[]
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Let these people find this skill in the catalog" — the access card's one
 * create action.
 *
 * One field, so one small `Dialog` rather than a wizard. The picker is
 * `UserAllowlistPicker`, a `Popover` anchored to its own input — never
 * `BundlePermissionsAddUserModal`, which is a `Dialog` and would stack
 * (guidelines R8).
 *
 * The route grants **one** user per call, so this posts once per person and
 * reports per person: a single toast over a batch where one address was
 * misspelled would tell the publisher that all of them landed.
 */
export function AddSkillPackageGrantDialog({
  packageId,
  packageName,
  existingUserIds,
  open,
  onOpenChange,
}: AddSkillPackageGrantDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast } = useCustomToast()
  const [people, setPeople] = useState<
    Array<{ id: string; email: string; label: string }>
  >([])
  // Carries the user id, not just the rendered line: pruning the retry list by
  // matching a formatted string's prefix would collide on emails that prefix
  // one another (`a@x.com` succeeding while `a@x.com.au` failed would leave the
  // successful one in the list to be posted again).
  const [failures, setFailures] = useState<Array<{ id: string; line: string }>>(
    [],
  )

  const addMutation = useMutation({
    // One request per person: the route grants a single email per call. The
    // loop deliberately cannot reject — a refusal for one address must not
    // discard the outcome of the others — so `onSuccess` always runs and the
    // result carries both halves.
    mutationFn: async () => {
      const failed: Array<{ id: string; line: string }> = []
      let added = 0
      for (const person of people) {
        try {
          await SkillsService.addSkillPackageGrant({
            packageId,
            requestBody: { email: person.email },
          })
          added += 1
        } catch (error) {
          const failure = skillCatalogFailure(
            error,
            `Couldn't add ${person.email}`,
          )
          failed.push({
            id: person.id,
            line: `${person.email} — ${failure.message}`,
          })
        }
      }
      return { added, failed }
    },
    onSuccess: ({ added, failed }) => {
      setFailures(failed)
      queryClient.invalidateQueries({
        queryKey: ["skills-catalog", "package", packageId, "grants"],
      })
      if (added > 0) {
        showSuccessToast(
          `${added} ${added === 1 ? "person" : "people"} can now find ${packageName}`,
        )
      }
      if (failed.length === 0) {
        setPeople([])
        onOpenChange(false)
      } else {
        // Keep only the ones that failed, so a retry does not re-add the rest.
        const failedIds = new Set(failed.map((entry) => entry.id))
        setPeople((prev) => prev.filter((person) => failedIds.has(person.id)))
      }
    },
  })

  const isPending = addMutation.isPending

  const selected: UserAllowlistSelectedItem[] = people.map((person) => ({
    id: person.id,
    userId: person.id,
    fallbackLabel: person.label,
  }))

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add people to {packageName}</DialogTitle>
          <DialogDescription>
            They can find and install this skill from the catalog. Removing
            someone later hides it from them — agents that already installed it
            keep working.
          </DialogDescription>
        </DialogHeader>

        <UserAllowlistPicker
          label={null}
          selected={selected}
          excludeUserIds={existingUserIds}
          isAdding={isPending}
          isRemoving={isPending}
          emptyHint="Search by name or email."
          onAdd={(user) => {
            // The alert below describes the *last* attempt; once the list
            // changes it no longer describes what is on screen.
            setFailures([])
            setPeople((prev) =>
              prev.some((person) => person.id === user.id)
                ? prev
                : [
                    ...prev,
                    {
                      id: user.id,
                      email: user.email,
                      label: user.full_name || user.email,
                    },
                  ],
            )
          }}
          onRemove={(item) => {
            setFailures([])
            setPeople((prev) => prev.filter((person) => person.id !== item.id))
          }}
        />

        {failures.length > 0 && (
          <Alert variant="destructive">
            <AlertDescription>
              {failures.map((failure) => (
                <p key={failure.id} className="text-xs break-words">
                  {failure.line}
                </p>
              ))}
            </AlertDescription>
          </Alert>
        )}

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={isPending}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <LoadingButton
            type="button"
            loading={isPending}
            disabled={people.length === 0}
            onClick={() => addMutation.mutate()}
          >
            Add
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
