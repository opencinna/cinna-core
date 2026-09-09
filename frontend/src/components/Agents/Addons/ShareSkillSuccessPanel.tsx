import { Link } from "@tanstack/react-router"
import { Upload } from "lucide-react"

import type { SkillPackageRevisionPublic } from "@/client"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { Button } from "@/components/ui/button"
import {
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { skillRevisionLabel } from "@/utils/skillCatalog"

interface ShareSkillSuccessPanelProps {
  skillName: string
  revision: SkillPackageRevisionPublic
  /** The package's reverse-DNS handle, once the package read has landed. */
  packageHandle: string | null
  onClose: () => void
}

/**
 * What the share dialog becomes once the skill is in the catalog.
 *
 * P6's success panel, built to `Admin/InviteSuccessPanel`'s rule: it **links
 * onward** to the package's page instead of opening a second dialog on top of
 * this one (guidelines R8, A2).
 *
 * The package id goes through `Common/CopyableValue` rather than a local copy
 * block — that primitive owns the tick timing, the `aria-label`, the tooltip
 * and the failure toast for every label-plus-value control in the product, and
 * its own contract asks new ones to use it.
 */
export function ShareSkillSuccessPanel({
  skillName,
  revision,
  packageHandle,
  onClose,
}: ShareSkillSuccessPanelProps) {
  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex min-w-0 items-center gap-2">
          <Upload className="h-5 w-5 shrink-0" />
          <span className="truncate">
            Shared {skillName} {skillRevisionLabel(revision)}
          </span>
        </DialogTitle>
        <DialogDescription>
          It is in the skills catalog now. Anyone who can see it can add it to
          one of their agents.
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-3">
        <Button asChild variant="link" className="h-auto px-0">
          <Link
            to="/catalog/skills/$packageId"
            params={{ packageId: revision.package_id }}
          >
            Open in the skills catalog
          </Link>
        </Button>
        {packageHandle && (
          <CopyableValue label="Package id" value={packageHandle} />
        )}
      </div>

      <DialogFooter>
        <Button type="button" onClick={onClose}>
          Close
        </Button>
      </DialogFooter>
    </>
  )
}
