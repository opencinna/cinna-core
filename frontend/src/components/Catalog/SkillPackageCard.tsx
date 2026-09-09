import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Download, EyeOff, GraduationCap, Pencil } from "lucide-react"
import { useState } from "react"

import type { SkillPackageDetailPublic } from "@/client"
import { SkillsService } from "@/client"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { PublisherEmailConfirmedIcon } from "@/components/Common/PublisherEmailConfirmedIcon"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
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
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  SKILL_VISIBILITY_OPTIONS,
  skillPackageVersionLabel,
  skillPublisherLabel,
} from "@/utils/skillCatalog"
import { AddSkillToAgentDialog } from "./AddSkillToAgentDialog"
import { EditSkillPackageDialog } from "./EditSkillPackageDialog"

interface SkillPackageCardProps {
  pkg: SkillPackageDetailPublic
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-muted-foreground shrink-0">{label}</span>
      <span className="text-sm font-medium truncate">{value}</span>
    </div>
  )
}

/**
 * "What is this package, and do I want it" — the left column of S7.
 *
 * Four blocks: the five facts, the copyable id, the primary action, and the
 * publisher's `⋯`. The publisher's verbs are in that menu and nowhere else
 * (A5): Edit and Delist are exactly the actions §1 "View" says must not be
 * visible controls on a surface whose story is *read this and decide*.
 */
export function SkillPackageCard({ pkg }: SkillPackageCardProps) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [addOpen, setAddOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [delistOpen, setDelistOpen] = useState(false)

  // Delisting is superuser-only server-side (`SkillCatalogService.delist`), and
  // no capability field reports it, so this is read off the account rather than
  // off the payload. Deliberately not `useRole().isAdmin`, which is also true
  // for the `admin` *role* without `is_superuser` — offering a verb the API
  // would refuse is worse than not offering it.
  const canDelist = !!user?.is_superuser && pkg.is_listed

  const delistMutation = useMutation({
    mutationFn: () => SkillsService.delistSkillPackage({ packageId: pkg.id }),
    onSuccess: () => {
      showSuccessToast("Package delisted")
      setDelistOpen(false)
      queryClient.invalidateQueries({ queryKey: ["skills-catalog"] })
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to delist the package")),
  })

  const versionLabel = skillPackageVersionLabel(pkg) ?? "No revision yet"
  // Three visibilities since `users` shipped, and a delisting on top of any of
  // them. The old two-branch expression printed "Private" for a package shared
  // with named people — telling its publisher the opposite of what they did.
  const visibilityLabel = (() => {
    const base =
      SKILL_VISIBILITY_OPTIONS.find((option) => option.value === pkg.visibility)
        ?.label ?? "Private"
    return pkg.is_listed ? base : `${base}, delisted`
  })()
  const revisionCount = pkg.revisions?.length ?? 0
  // Two different questions, so two facts. `install_count` is **other people**,
  // one per person however many agents they use it in; `installed_in_agent_ids`
  // is the viewer's own agents. A publisher who has just installed their own
  // skill in three of their agents is 0 and 3 — true, and unreadable as a
  // single number, which is why there is not one.
  const catalogInstalls = pkg.install_count ?? 0
  const myInstalls = pkg.installed_in_agent_ids?.length ?? 0

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <GraduationCap className="h-5 w-5 shrink-0" />
            Package
          </CardTitle>
          {(pkg.can_manage || canDelist) && (
            <div className="shrink-0">
              <RowActionsMenu
                label={`the package ${pkg.display_name}`}
                disabled={delistMutation.isPending}
              >
                {pkg.can_manage && (
                  <DropdownMenuItem
                    onSelect={(e) => {
                      // The item unmounts on select and would take the dialog's
                      // own pending state with it.
                      e.preventDefault()
                      setEditOpen(true)
                    }}
                  >
                    <Pencil />
                    Edit details…
                  </DropdownMenuItem>
                )}
                {pkg.can_manage && canDelist && <DropdownMenuSeparator />}
                {canDelist && (
                  <DropdownMenuItem
                    onSelect={(e) => {
                      e.preventDefault()
                      setDelistOpen(true)
                    }}
                  >
                    <EyeOff />
                    Delist from the catalog
                  </DropdownMenuItem>
                )}
              </RowActionsMenu>
            </div>
          )}
        </div>
        <CardDescription>
          {pkg.description || "This package has no description."}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-xs text-muted-foreground shrink-0">
              Publisher
            </span>
            <span className="flex min-w-0 items-center gap-1">
              <span className="truncate text-sm font-medium">
                {skillPublisherLabel(pkg)}
              </span>
              <PublisherEmailConfirmedIcon
                confirmed={pkg.publisher_email_confirmed ?? false}
                hasEmail={!!pkg.publisher_email}
              />
            </span>
          </div>
          <Fact label="Latest version" value={versionLabel} />
          <Fact label="Revisions" value={String(revisionCount)} />
          {/* People, not agents, and never the publisher — so a publisher
              dogfooding their skill does not read this as adoption. */}
          <Fact label="Catalog installs" value={String(catalogInstalls)} />
          {myInstalls > 0 && (
            <Fact label="Used in my agents" value={String(myInstalls)} />
          )}
          <Fact label="Visibility" value={visibilityLabel} />
        </div>

        {/* Only for the publisher, and only when the two numbers disagree:
            "Installs 0" beside "In your agents 3" is a contradiction until
            somebody says which agents each one counts. */}
        {pkg.can_manage && myInstalls > 0 && (
          <p className="text-xs text-muted-foreground">
            Catalog installs counts other people, one each — your own agents are
            never counted, so dogfooding cannot inflate it.
          </p>
        )}

        <CopyableValue label="Package id" value={pkg.package_id} />

        <Button className="w-full" onClick={() => setAddOpen(true)}>
          <Download className="h-4 w-4 mr-2" />
          Add to agent
        </Button>
      </CardContent>

      {addOpen && (
        <AddSkillToAgentDialog
          packageId={pkg.id}
          packageName={pkg.display_name}
          open
          onOpenChange={setAddOpen}
        />
      )}
      {editOpen && (
        <EditSkillPackageDialog pkg={pkg} open onOpenChange={setEditOpen} />
      )}

      {/* Owned by the card rather than nested in the menu item that opens it:
          a `DropdownMenuItem` unmounts on select and would take the confirm's
          pending state with it. Delisting is reversible by the publisher
          (`is_listed` on the edit dialog), so the confirm names the package and
          says what it does rather than warning about permanence. */}
      <AlertDialog
        open={delistOpen}
        onOpenChange={(next) => {
          if (!delistMutation.isPending) setDelistOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delist {pkg.display_name}?</AlertDialogTitle>
            <AlertDialogDescription>
              The package disappears from the skills catalog for everyone. It is
              not deleted, and agents that already installed it keep working —
              their link points at a revision, which delisting does not touch.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={delistMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                delistMutation.mutate()
              }}
              disabled={delistMutation.isPending}
            >
              {delistMutation.isPending ? "Delisting…" : "Delist package"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
