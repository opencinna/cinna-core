import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { GraduationCap, KeyRound, Link2Off, Monitor } from "lucide-react"
import type { ReactNode } from "react"

import { type SkillPackageDetailPublic, SkillsService } from "@/client"
import { detectPlatform } from "@/components/Desktop/DesktopDownloadSection"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { isLoggedIn } from "@/hooks/useAuth"
import { APP_NAME } from "@/utils"
import { skillPublisherLabel } from "@/utils/skillCatalog"
import { credentialTypeLabel } from "@/utils/skillCredentials"
import {
  cinnaSkillDeepLink,
  isValidSkillLinkPackageId,
} from "@/utils/skillLinks"

/** Full-viewport centring, the `/desktop` page's shell. */
function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-4 bg-background">
      <Card className="w-full max-w-md">{children}</Card>
    </div>
  )
}

/** Which instance the link points at: the visitor may use several. */
function InstanceLine() {
  return (
    <p className="text-xs text-muted-foreground">
      {APP_NAME} · {window.location.host}
    </p>
  )
}

/**
 * `Version N (label)`: the revision number is what changes on every publish,
 * the free-text label is the publisher's own name for it.
 */
function latestVersionLabel(pkg: SkillPackageDetailPublic): string | null {
  const n = pkg.latest_revision_number
  if (n == null) return null
  return pkg.latest_version
    ? `Version ${n} (v${pkg.latest_version})`
    : `Version ${n}`
}

/**
 * What a signed-in visitor who may see the package also gets: the facts they
 * would decide on, and the way into the catalog page.
 */
function PackageDetails({ pkg }: { pkg: SkillPackageDetailPublic }) {
  const latest = pkg.revisions?.find((rev) => rev.id === pkg.latest_revision_id)
  const needs = latest?.required_credentials ?? []
  const version = latestVersionLabel(pkg)

  return (
    <section
      aria-label="Skill details"
      className="space-y-3 rounded-lg border p-4"
    >
      <div className="min-w-0">
        <h2 className="text-base font-semibold break-words">
          {pkg.display_name}
        </h2>
        <p className="truncate text-xs text-muted-foreground">
          by {skillPublisherLabel(pkg)}
          {version && ` · ${version}`}
        </p>
      </div>
      {pkg.description && (
        <p className="text-sm text-muted-foreground">{pkg.description}</p>
      )}
      {needs.length > 0 && (
        <div className="space-y-1">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <KeyRound className="h-3 w-3 shrink-0" />
            Needs
          </p>
          <ul className="space-y-0.5 text-xs text-muted-foreground">
            {needs.map((req) => (
              <li key={req.slot} className="truncate">
                <span className="font-mono">{req.slot}</span> ·{" "}
                {credentialTypeLabel(req.type)}
              </li>
            ))}
          </ul>
        </div>
      )}
      <Button asChild variant="outline" size="sm" className="w-full">
        <Link to="/catalog/skills/$packageId" params={{ packageId: pkg.id }}>
          View in catalog
        </Link>
      </Button>
    </section>
  )
}

function InvalidSkillLink() {
  return (
    <Shell>
      <CardHeader className="text-center">
        <Link2Off className="mx-auto h-12 w-12 text-muted-foreground" />
        <CardTitle className="mt-2">This link isn't valid</CardTitle>
        <CardDescription>
          Check that you copied the whole link, or ask whoever shared it to send
          it again.
        </CardDescription>
        <InstanceLine />
      </CardHeader>
    </Shell>
  )
}

/**
 * `/get/skill/<package_id>` — what someone opens after being sent a skill.
 *
 * Public on purpose (no login wall, like `/desktop`): the recipient may not
 * have an account here. The page they see signed out is the same for every
 * well-formed id, so it never says whether a private package exists. A
 * signed-in visitor who may see the package also gets its details; one who
 * may not gets the signed-out page exactly, because the lookup answers 404
 * for both a missing package and an invisible one and this page renders a
 * failed lookup as nothing at all.
 */
export function SkillLinkPage({ packageId }: { packageId: string }) {
  const valid = isValidSkillLinkPackageId(packageId)
  const { data: pkg } = useQuery({
    queryKey: ["skill-link", packageId],
    queryFn: () => SkillsService.getSkillPackageByPackageId({ packageId }),
    enabled: valid && isLoggedIn(),
    retry: false,
    refetchOnWindowFocus: false,
  })

  if (!valid) return <InvalidSkillLink />

  return (
    <Shell>
      <CardHeader className="text-center">
        <GraduationCap className="mx-auto h-12 w-12 text-primary" />
        <CardTitle className="mt-2">
          Someone shared a Cinna skill with you
        </CardTitle>
        <InstanceLine />
        <p className="mt-1 break-all font-mono text-sm">{packageId}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {pkg && <PackageDetails pkg={pkg} />}
        <div className="space-y-2">
          <Button asChild className="w-full">
            <a href={cinnaSkillDeepLink(packageId)}>
              <Monitor className="h-4 w-4 mr-2" />
              Open in Cinna Desktop
            </a>
          </Button>
          {/* The app never reads the clipboard on macOS (the system paste
              prompt), so a paste is the person's own move there. */}
          {detectPlatform() === "darwin" && (
            <p className="text-center text-xs text-muted-foreground">
              or paste this page's link into Cinna Desktop
            </p>
          )}
        </div>
        <Separator />
        <p className="text-center text-xs text-muted-foreground">
          Don't have the app yet?{" "}
          <Link to="/desktop" className="font-medium underline">
            Get Cinna Desktop
          </Link>
        </p>
      </CardContent>
    </Shell>
  )
}
