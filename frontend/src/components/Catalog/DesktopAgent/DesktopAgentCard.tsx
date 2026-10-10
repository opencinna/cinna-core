import { GraduationCap, History, KeyRound, Laptop } from "lucide-react"
import { type ReactNode, useState } from "react"

import type { CatalogItemDetail, CatalogRevision } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import {
  catalogAccessFact,
  catalogCredentialRequirements,
  catalogNeedsLine,
  catalogVersionLabel,
  cinnaCatalogLink,
  myComputerCount,
  peopleCount,
} from "@/utils/catalogItems"
import { CatalogItemMenu } from "../CatalogItemMenu"
import { CatalogRevisionsSheet } from "../CatalogRevisionsSheet"
import { CatalogSkillsSheet } from "../CatalogSkillsSheet"
import { SkillRevisionCredentialsSheet } from "../SkillRevisionCredentialsSheet"

const DOOR_CLASS =
  "flex min-w-0 items-center gap-1.5 rounded-sm text-sm font-medium underline-offset-4 outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"

function FactLine({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="shrink-0 text-xs text-muted-foreground">{label}</span>
      {children}
    </div>
  )
}

function Value({ children }: { children: ReactNode }) {
  return <span className="truncate text-sm font-medium">{children}</span>
}

/** A fact whose value opens the list behind it — a door costs no height. */
function Door({
  value,
  icon: Icon,
  ariaLabel,
  onOpen,
}: {
  value: string
  icon: typeof History
  ariaLabel: string
  onOpen: () => void
}) {
  return (
    <button
      type="button"
      aria-haspopup="dialog"
      aria-label={ariaLabel}
      onClick={onOpen}
      className={DOOR_CLASS}
    >
      <span className="truncate">{value}</span>
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    </button>
  )
}

interface DesktopAgentCardProps {
  item: CatalogItemDetail
  /** The version the release-notes card shows; `null` before the first share. */
  shownRevision: CatalogRevision | null
  onSelectRevision: (revisionNumber: number) => void
}

/**
 * "What is this agent, who shares it, what does it need?" — the left column
 * of the desktop agent's detail route (S2). Three blocks: the facts, the
 * copyable catalog id, and the one way to get it. No install button: it runs
 * on the reader's computer, and Cinna Desktop is where it is fetched.
 */
export function DesktopAgentCard({
  item,
  shownRevision,
  onSelectRevision,
}: DesktopAgentCardProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [credentialsOpen, setCredentialsOpen] = useState(false)

  const revisions = item.revisions ?? []
  const versionLabel = shownRevision ? catalogVersionLabel(shownRevision) : null
  // Skills and credentials are the latest version's (the item carries no
  // per-version copy), so their Sheets are titled by the latest, not the shown.
  const latestLabel = item.latest
    ? catalogVersionLabel(item.latest)
    : "this agent"
  const skills = item.skills ?? []
  const needs = catalogNeedsLine(item.required_credential_specs)
  const requirements = catalogCredentialRequirements(
    item.required_credential_specs,
  )
  const computers = myComputerCount(item)
  const engine = item.desktop?.engine || null

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(item.id)
      showSuccessToast("Catalog id copied")
    } catch {
      showErrorToast("Couldn't copy to the clipboard")
    }
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex min-w-0 items-center gap-2">
            <Laptop className="h-5 w-5 shrink-0" />
            Agent
          </CardTitle>
          <div className="shrink-0">
            <CatalogItemMenu
              item={item}
              canMaintain={item.access === "maintain"}
            />
          </div>
        </div>
        <CardDescription className="break-words">
          {item.description || "This agent has no description."}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <FactLine label="Publisher">
            <Value>{item.publisher?.display_name || "Unknown person"}</Value>
          </FactLine>
          <FactLine label="Where it runs">
            <Value>On your computer</Value>
          </FactLine>
          <FactLine label="Version">
            {versionLabel && revisions.length > 0 ? (
              <Door
                value={versionLabel}
                icon={History}
                ariaLabel={`${versionLabel}. Show every version`}
                onOpen={() => setVersionsOpen(true)}
              />
            ) : (
              <Value>Nothing shared yet</Value>
            )}
          </FactLine>
          {engine && (
            <FactLine label="Engine">
              <Value>{engine}</Value>
            </FactLine>
          )}
          <FactLine label="Skills">
            {skills.length > 0 ? (
              <Door
                value={`${skills.length} ${skills.length === 1 ? "skill" : "skills"}`}
                icon={GraduationCap}
                ariaLabel={`${skills.length} skills. Show every skill`}
                onOpen={() => setSkillsOpen(true)}
              />
            ) : (
              <Value>None</Value>
            )}
          </FactLine>
          <FactLine label="Credentials">
            {needs ? (
              <Door
                value={needs}
                icon={KeyRound}
                ariaLabel={`${needs}. Show every credential`}
                onOpen={() => setCredentialsOpen(true)}
              />
            ) : (
              <Value>None declared</Value>
            )}
          </FactLine>
          <FactLine label="Catalog installs">
            <Value>{peopleCount(item.install_count ?? 0)}</Value>
          </FactLine>
          {computers > 0 && (
            <FactLine label="My computers">
              <Value>
                {computers} {computers === 1 ? "computer" : "computers"}
              </Value>
            </FactLine>
          )}
          <FactLine label="Access">
            <Value>{catalogAccessFact(item)}</Value>
          </FactLine>
        </div>

        <FactLine label="Catalog id">
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="max-w-full cursor-pointer truncate font-mono text-sm hover:underline"
                aria-label="Copy the catalog id"
                onClick={copyId}
              >
                {item.id}
              </button>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Click to copy
            </TooltipContent>
          </Tooltip>
        </FactLine>

        <div className="space-y-1.5">
          <Button className="w-full" asChild>
            <a href={cinnaCatalogLink(item)}>
              <Laptop className="mr-2 h-4 w-4" />
              Open in Cinna Desktop
            </a>
          </Button>
          <p className="text-center text-xs text-muted-foreground">
            Needs Cinna Desktop on this computer.
          </p>
        </div>
      </CardContent>

      <CatalogRevisionsSheet
        revisions={revisions}
        latestRevisionNumber={item.latest?.revision_number ?? null}
        onView={onSelectRevision}
        open={versionsOpen}
        onOpenChange={setVersionsOpen}
      />
      <CatalogSkillsSheet
        skills={skills}
        versionLabel={latestLabel}
        open={skillsOpen}
        onOpenChange={setSkillsOpen}
      />
      <SkillRevisionCredentialsSheet
        requirements={requirements}
        revisionLabel={latestLabel}
        description="What this agent needs on your computer. The publisher's credentials are shared with you; you add the rest in Cinna Desktop."
        open={credentialsOpen}
        onOpenChange={setCredentialsOpen}
      />
    </Card>
  )
}
