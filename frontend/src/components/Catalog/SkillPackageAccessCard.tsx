import { useQuery } from "@tanstack/react-query"
import { UserPlus, Users } from "lucide-react"
import { useState } from "react"

import type { SkillPackageDetailPublic } from "@/client"
import { SkillsService } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AddSkillPackageGrantDialog } from "./AddSkillPackageGrantDialog"
import { AllSkillPackageGrantsSheet } from "./AllSkillPackageGrantsSheet"
import { SkillPackageGrantRow } from "./SkillPackageGrantRow"

interface SkillPackageAccessCardProps {
  pkg: SkillPackageDetailPublic
}

/**
 * "Who can find this skill, and take one person off the list" — S5.
 *
 * A P5 preview list with the connected-machines action treatment: five rows, a
 * "Show all (N)" Sheet, no status dot (membership *is* access) and one
 * hover-revealed destructive verb per row instead of a `⋯` holding one item.
 *
 * It renders for the publisher when the package is `users`-visible **or** when
 * grants still exist. The second clause is not decoration: flipping `users` →
 * `public` keeps the grants inert rather than deleting them (plan §9), and a
 * card that vanishes while the list still exists is how a publisher loses
 * track of it.
 */
export function SkillPackageAccessCard({ pkg }: SkillPackageAccessCardProps) {
  const [sheetOpen, setSheetOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["skills-catalog", "package", pkg.id, "grants"],
    queryFn: () => SkillsService.listSkillPackageGrants({ packageId: pkg.id }),
    // Publisher-only server-side; asking as anyone else is a guaranteed 403.
    enabled: !!pkg.can_manage,
  })

  const grants = data?.data ?? []
  const isUsersVisibility = pkg.visibility === "users"

  // The route renders this only when it should exist at all; this second gate
  // is what keeps it from disappearing the moment the publisher goes public.
  if (!pkg.can_manage) return null
  if (!isUsersVisibility && grants.length === 0) return null

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex min-w-0 items-center gap-2">
            <Users className="h-5 w-5 shrink-0" />
            Who can see this
          </CardTitle>
          <div className="shrink-0">
            <Button size="sm" onClick={() => setAddOpen(true)}>
              <UserPlus className="mr-2 h-4 w-4" />
              Add person
            </Button>
          </div>
        </div>
        <CardDescription>
          Only these people can find this skill in the catalog. Removing someone
          hides it from their catalog — agents that already installed it keep
          working.
          {!isUsersVisibility && (
            <> This skill is public, so these people have access anyway.</>
          )}
        </CardDescription>
      </CardHeader>

      <CardContent>
        <PreviewList
          items={grants}
          total={data?.count}
          getKey={(grant) => grant.id}
          renderItem={(grant) => (
            <SkillPackageGrantRow packageId={pkg.id} grant={grant} />
          )}
          isLoading={isLoading}
          // A failed background refetch keeps the list on screen; a first
          // failed load renders as a failure, never as "no one yet".
          isError={isError && !data}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't load who can see this"
          empty={
            <p className="text-sm text-muted-foreground">
              No one yet — add a person so they can find this skill in the
              catalog.
            </p>
          }
          onShowAll={() => setSheetOpen(true)}
        />
      </CardContent>

      <AllSkillPackageGrantsSheet
        packageId={pkg.id}
        grants={grants}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />

      {addOpen && (
        <AddSkillPackageGrantDialog
          packageId={pkg.id}
          packageName={pkg.display_name}
          existingUserIds={grants.map((grant) => grant.user_id)}
          open
          onOpenChange={setAddOpen}
        />
      )}
    </Card>
  )
}
