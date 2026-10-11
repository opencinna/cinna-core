/**
 * Where a credential is published: the bundle and skill rows of its
 * disable-sharing and delete confirms and of the Sharing card (S6).
 *
 * `ListRow`s, replacing the bordered `li`s both files hand-rolled (A10). No
 * dot, no menu. The one control opens the package in a **new tab**, so a
 * confirm is never left by navigating out of it.
 */
import { Bot, Box, GraduationCap, Laptop, type LucideIcon } from "lucide-react"

import type {
  CredentialBundleUsage,
  CredentialShareAgentUsage,
  CredentialShareLocalSkillUsage,
  CredentialSkillUsage,
} from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { NewTabIconLink } from "@/components/Common/NewTabIconLink"
import { providedByPublisherLabel } from "@/utils/skillCredentials"

function UsageTile({ icon: Icon }: { icon: LucideIcon }) {
  return (
    <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
      <Icon className="h-3.5 w-3.5 text-muted-foreground" />
    </span>
  )
}

interface BundleUsageRowProps {
  usage: CredentialBundleUsage
  /** Append the provisioning mode to the meta line (lists that mix modes). */
  showProvidedBy?: boolean
}

export function BundleUsageRow({ usage, showProvidedBy }: BundleUsageRowProps) {
  return (
    <ListRow
      icon={<UsageTile icon={Box} />}
      title={usage.display_name}
      meta={
        <>
          <span className="font-mono">{usage.bundle_id}</span>
          {showProvidedBy &&
            ` · ${providedByPublisherLabel(usage.provided_by)}`}
        </>
      }
    >
      {usage.publisher_install_id && (
        <NewTabIconLink
          label={`Open ${usage.display_name} in a new tab`}
          linkOptions={{
            to: "/agent/$agentId",
            params: { agentId: usage.publisher_install_id },
            hash: "bundle",
          }}
        />
      )}
    </ListRow>
  )
}

export function SkillUsageRow({ usage }: { usage: CredentialSkillUsage }) {
  const revisions = usage.revision_numbers ?? []
  return (
    <ListRow
      icon={<UsageTile icon={GraduationCap} />}
      title={usage.display_name}
      meta={<span className="font-mono">{usage.package_id}</span>}
      flags={
        revisions.length > 0 ? (
          <RowInfo
            facts={[
              `Provided in ${revisions.length === 1 ? "revision" : "revisions"} ${revisions.join(", ")}`,
            ]}
          />
        ) : undefined
      }
    >
      <NewTabIconLink
        label={`Open ${usage.display_name} in a new tab`}
        linkOptions={{
          to: "/catalog/skills/$packageId",
          params: { packageId: usage.package_uuid },
        }}
      />
    </ListRow>
  )
}

/**
 * What one share recipient loses when the owner unshares from them: a cloud
 * agent of theirs that uses it through one of the owner's skills (agents
 * that use it directly are only counted, by the caller). No link — it is not the
 * owner's agent to open.
 */
export function RecipientAgentRow({
  usage,
}: {
  usage: CredentialShareAgentUsage
}) {
  const skills = usage.skill_display_names ?? []
  return (
    <ListRow
      icon={<UsageTile icon={Bot} />}
      title={usage.name}
      meta={
        skills.length > 0 ? `Uses it through ${skills.join(", ")}` : undefined
      }
    />
  )
}

/** A skill of the owner the recipient installed on their own computer. */
export function RecipientLocalSkillRow({
  usage,
}: {
  usage: CredentialShareLocalSkillUsage
}) {
  return (
    <ListRow
      icon={<UsageTile icon={GraduationCap} />}
      title={usage.display_name}
      meta={usage.host_agent_name ? `in ${usage.host_agent_name}` : undefined}
      flags={<RowFlag icon={Laptop} label="On their computer" />}
    />
  )
}
