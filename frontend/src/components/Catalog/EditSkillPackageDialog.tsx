import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import type { SkillPackageDetailPublic } from "@/client"
import { SkillsService } from "@/client"
import { TooltipToggleItem } from "@/components/Common/TooltipToggleItem"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup } from "@/components/ui/toggle-group"
import useCustomToast from "@/hooks/useCustomToast"
import { SKILL_VISIBILITY_OPTIONS } from "@/utils/skillCatalog"
import { SkillCatalogErrorAlert } from "./SkillCatalogErrorAlert"

interface EditSkillPackageDialogProps {
  pkg: SkillPackageDetailPublic
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Rename it, retitle it, or make it public" — S8.
 *
 * The Edit story, split from S7's View per §1: the package route reads, this
 * dialog writes. One section, four fields, explicit Save / Cancel with dirty
 * tracking — nothing here persists on change, including the `is_listed`
 * `Switch`, which is a form field like the others and not an auto-save control.
 */
export function EditSkillPackageDialog({
  pkg,
  open,
  onOpenChange,
}: EditSkillPackageDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast } = useCustomToast()

  const [displayName, setDisplayName] = useState(pkg.display_name)
  const [description, setDescription] = useState(pkg.description ?? "")
  const [visibility, setVisibility] = useState(pkg.visibility)
  const [isListed, setIsListed] = useState(pkg.is_listed)

  const isDirty =
    displayName.trim() !== pkg.display_name ||
    description !== (pkg.description ?? "") ||
    visibility !== pkg.visibility ||
    isListed !== pkg.is_listed

  const saveMutation = useMutation({
    mutationFn: () =>
      SkillsService.updateSkillPackage({
        packageId: pkg.id,
        requestBody: {
          display_name: displayName.trim(),
          description: description.trim() || null,
          visibility,
          is_listed: isListed,
        },
      }),
    onSuccess: () => {
      showSuccessToast("Package updated")
      queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
      onOpenChange(false)
    },
    // The refusal is coded (`invalid_display_name`, `invalid_visibility`) and
    // belongs beside the field that caused it, not in a toast.
  })

  const isPending = saveMutation.isPending

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Edit package details</DialogTitle>
          <DialogDescription>
            How this package appears in the skills catalog. The package id and
            its published revisions never change.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="skill-package-name">Display name</Label>
            <Input
              id="skill-package-name"
              value={displayName}
              disabled={isPending}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="skill-package-description">Description</Label>
            <Textarea
              id="skill-package-description"
              rows={3}
              value={description}
              disabled={isPending}
              placeholder="What this skill does, in one or two sentences."
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <Label>Visibility</Label>
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={visibility}
              disabled={isPending}
              // A single-select group can be emptied by clicking the active
              // segment; visibility has no "unset", so an empty value keeps the
              // current one rather than sending `null`.
              onValueChange={(next) => next && setVisibility(next)}
              aria-label="Who can install this package"
            >
              {SKILL_VISIBILITY_OPTIONS.map((option) => (
                <TooltipToggleItem
                  key={option.value}
                  value={option.value}
                  label={option.hint}
                >
                  {option.label}
                </TooltipToggleItem>
              ))}
            </ToggleGroup>
          </div>

          <div className="flex items-center justify-between gap-3">
            <Label htmlFor="skill-package-listed" className="font-normal">
              Listed in the catalog
            </Label>
            <Switch
              id="skill-package-listed"
              checked={isListed}
              disabled={isPending}
              onCheckedChange={setIsListed}
            />
          </div>

          {saveMutation.isError && (
            <SkillCatalogErrorAlert
              error={saveMutation.error}
              fallback="Couldn't update the package"
            />
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            type="button"
            loading={isPending}
            disabled={!isDirty || !displayName.trim()}
            onClick={() => saveMutation.mutate()}
          >
            Save changes
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
