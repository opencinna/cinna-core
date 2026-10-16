/**
 * Words for catalog-skill credential slots — the **only** place the surfaces of
 * the skill credential requirements feature read copy from (plan §9.3 S0):
 * Share skill, Add skill to agent, the Add addon wizard, the catalog tile and
 * package card, the Addons row and detail dialog, and the credential impact
 * confirms. One module so the same slot cannot read one way before an install
 * and another way after it (§2 "The same entity, before and after").
 *
 * Every table is a `Record` keyed by the **generated** union, so a value the
 * server adds is a missing key here and fails to compile rather than printing a
 * raw code (the `FORMAT_LABEL` precedent in `utils/addons.ts`). Wire-string
 * fallbacks use `||`: an absent name arrives as `""` as often as `null`.
 *
 * **Two perspectives on `provided_by`.** Every surface that installs a skill or
 * reads a published revision speaks to the *installer* (`PROVIDED_BY_COPY`:
 * "From the publisher", "You provide it"). The Share skill dialog is the one
 * surface whose reader *is* the publisher, where "From the publisher" would
 * name them in the third person and "You provide it" would be false — it reads
 * `PROVIDED_BY_PUBLISHER_COPY` instead ("Shared with installers"), as do the
 * bundle usage rows of a credential's impact dialogs, whose reader is also the
 * publisher. Do not mix the two on a surface.
 */
import type {
  AddonCredentialIssuePublic,
  CredentialType,
  SkillCredentialProvisionPublic,
  SkillCredentialRequirementPublic,
  SkillPublishCredentialPreview,
} from "@/client"
import type { RowStatus } from "@/components/Common/ListRow"

export type SlotProvidedBy = NonNullable<
  SkillCredentialRequirementPublic["provided_by"]
>
export type SlotOutcome = SkillCredentialProvisionPublic["outcome"]
export type PublishReason = NonNullable<SkillPublishCredentialPreview["reason"]>
export type IssueReason = AddonCredentialIssuePublic["reason"]
/** Whether a provisioning sentence speaks before the install or after it. */
export type SlotPhase = "preview" | "installed"
/** Which confirm an impact sentence is for. */
export type ImpactAction = "disable" | "delete"

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many
}

/**
 * Credential types in the words a skill's slot row uses. (The credentials list
 * itself reads `Credentials/credentialTypes.getCredentialTypeMeta`, the type
 * picker's table.)
 */
const CREDENTIAL_TYPE_LABELS: Record<CredentialType, string> = {
  email_imap: "Email (IMAP)",
  email_smtp: "Email (SMTP)",
  odoo: "Odoo",
  gmail_oauth: "Gmail OAuth",
  gmail_oauth_readonly: "Gmail OAuth (Read-Only)",
  gdrive_oauth: "Google Drive OAuth",
  gdrive_oauth_readonly: "Google Drive OAuth (Read-Only)",
  gcalendar_oauth: "Google Calendar OAuth",
  gcalendar_oauth_readonly: "Google Calendar OAuth (Read-Only)",
  gmeet_oauth: "Google Meet OAuth",
  gmeet_oauth_readonly: "Google Meet OAuth (Read-Only)",
  google_service_account: "Google Service Account",
  api_token: "API Token",
  ssh_key: "SSH Key",
  agent_api: "Agent REST API",
  mcp_provider: "MCP Provider",
}

// Read through a `Map` rather than indexing the object with a wire string: the
// table cannot drift from the union, but the value may be newer than this build
// (and `Object.hasOwn` is outside this project's ES2020 lib).
const CREDENTIAL_TYPE_LABEL_BY_TYPE = new Map<string, string>(
  Object.entries(CREDENTIAL_TYPE_LABELS),
)

/** The type's label; a type this build has not heard of prints as sent. */
export function credentialTypeLabel(type: string): string {
  return CREDENTIAL_TYPE_LABEL_BY_TYPE.get(type) || type
}

/** `provided_by` is optional on the wire; the server's default is `user`. */
export function slotProvidedBy(
  providedBy: SlotProvidedBy | null | undefined,
): SlotProvidedBy {
  return providedBy || "user"
}

/**
 * Who supplies a slot, **to the installer**: a short form for a row's meta
 * line, and the sentence.
 */
export const PROVIDED_BY_COPY: Record<
  SlotProvidedBy,
  { short: string; sentence: string }
> = {
  publisher: {
    short: "From the publisher",
    sentence: "Provided by the publisher",
  },
  template: {
    short: "Publisher's template",
    sentence: "Publisher's template — you add the secret",
  },
  user: { short: "You provide it", sentence: "You provide it" },
}

/** Who supplies a slot, **to the publisher** — the Share skill dialog only. */
export const PROVIDED_BY_PUBLISHER_COPY: Record<
  SlotProvidedBy,
  { short: string }
> = {
  publisher: { short: "Shared with installers" },
  template: { short: "Shared as a template" },
  user: { short: "Installers provide it" },
}

const PROVIDED_BY_PUBLISHER_SHORT = new Map<string, string>(
  Object.entries(PROVIDED_BY_PUBLISHER_COPY).map(([key, copy]) => [
    key,
    copy.short,
  ]),
)

/**
 * The publisher-voice label for an **untyped** wire `provided_by` (the
 * credential's bundle usage rows carry a bare string). Absent reads as the
 * server's default, `user`; a value this build has not heard of prints as sent.
 */
export function providedByPublisherLabel(
  providedBy: string | null | undefined,
): string {
  if (!providedBy) return PROVIDED_BY_PUBLISHER_COPY.user.short
  return PROVIDED_BY_PUBLISHER_SHORT.get(providedBy) || providedBy
}

/*
 * Share skill (S1): why a slot resolved to "installers bring their own".
 *
 * Every `template_would_leak_secret` fix is reachable from the private-field
 * picker: since D17 the backend waives a computed secret whose stored source is
 * private, so marking the fields the picker offers is always enough.
 */
const PUBLISH_REASON_SENTENCE: Record<
  PublishReason,
  (subject: string, object: string) => string
> = {
  no_linked_credential: () =>
    "No credential with this slot is linked to this agent, so installers bring their own.",
  not_shareable: (_subject, object) =>
    `Sharing is off on ${object}, so installers bring their own. Turn sharing on to provide it.`,
  not_owned: (subject) =>
    `${subject} belongs to someone else. Only credentials you own can be provided to installers.`,
  template_would_leak_secret: (subject) =>
    `${subject} would ship its secret inside the published skill. Mark its secret fields private to provide it as a template, or turn sharing on.`,
}

export function publishReasonSentence(
  reason: PublishReason,
  credentialName: string | null | undefined,
): string {
  return PUBLISH_REASON_SENTENCE[reason](
    credentialName || "The credential",
    credentialName || "the credential",
  )
}

/**
 * "Backed by agent X" for an `agent_api` slot (brief §4) — the fact that says
 * which agent the connection behind the slot talks to.
 *
 * The name is frozen into the revision at publish beside `producer_agent_id`,
 * so it is the name as it read then: a later rename does not reach a published
 * revision, and a revision published before the name was frozen sends none, in
 * which case there is no line rather than a bare id.
 */
export function producerAgentFact(
  producerAgentName: string | null | undefined,
): string | null {
  return producerAgentName ? `Backed by agent ${producerAgentName}` : null
}

/** Tooltip and `aria-label` of S1's open-the-credential control. */
export function publishOpenLabel(
  reason: PublishReason | null | undefined,
  credentialName: string | null | undefined,
): string {
  const name = credentialName || "the credential"
  if (reason === "not_shareable") {
    return `Open ${name} to turn sharing on (new tab)`
  }
  if (reason === "template_would_leak_secret") {
    return `Open ${name} to mark its secret fields private (new tab)`
  }
  return `Open ${name} (new tab)`
}

interface SlotCopyParams {
  slot: string
  credentialName?: string | null
}

interface SlotOutcomeCopy {
  /** The slot will not work until the user fills something in. */
  needsSetup: boolean
  /** Meta-line form; reads right before and after the install. */
  short: (p: SlotCopyParams) => string
  preview: (p: SlotCopyParams) => string
  installed: (p: SlotCopyParams) => string
}

/** Install preview and install report (S2/S3): what happens to each slot. */
export const SLOT_OUTCOME_COPY: Record<SlotOutcome, SlotOutcomeCopy> = {
  already_linked: {
    needsSetup: false,
    short: () => "Already linked",
    preview: () => "Already linked on this agent",
    installed: () => "Already linked on this agent",
  },
  linked_publisher: {
    needsSetup: false,
    short: () => "Shared by the publisher",
    preview: () =>
      "The publisher's credential will be shared with you and linked",
    installed: () =>
      "The publisher's credential was shared with you and linked",
  },
  linked_existing: {
    needsSetup: false,
    short: (p) =>
      p.credentialName
        ? `Uses your ${p.credentialName}`
        : "Uses your credential",
    preview: (p) =>
      `Your credential ${p.credentialName || "for this slot"} will be linked`,
    installed: (p) =>
      `Your credential ${p.credentialName || "for this slot"} was linked`,
  },
  template_materialised: {
    needsSetup: true,
    short: () => "From template — needs the secret",
    preview: () =>
      "A credential will be created from the publisher's template — fill in the secret afterwards",
    installed: () =>
      "A credential was created from the publisher's template — fill in the secret",
  },
  placeholder_created: {
    needsSetup: true,
    short: () => "Empty — needs filling in",
    preview: (p) =>
      `An empty credential ${p.slot} will be created — fill it in on the agent's Credentials tab`,
    installed: (p) =>
      `An empty credential ${p.slot} was created — fill it in on the agent's Credentials tab`,
  },
  publisher_unavailable: {
    needsSetup: true,
    short: () => "Publisher's unavailable — needs filling in",
    preview: () =>
      "The publisher's credential is unavailable — an empty one will be created for you to fill in",
    installed: () =>
      "The publisher's credential was unavailable — an empty one was created for you to fill in",
  },
}

type ProvisionItem = Pick<
  SkillCredentialProvisionPublic,
  "slot" | "outcome" | "credential_name" | "needs_setup"
>

type ProvisionState = Pick<
  SkillCredentialProvisionPublic,
  "outcome" | "needs_setup"
>

/** Reusing an existing credential may still require setup (e.g. a placeholder). */
export function slotOutcomeCopy(item: ProvisionState): SlotOutcomeCopy {
  const copy = SLOT_OUTCOME_COPY[item.outcome]
  const needsSetup = item.needs_setup ?? copy.needsSetup
  if (needsSetup && !copy.needsSetup) {
    return {
      needsSetup,
      short: (p) => `${copy.short(p)} — needs setup`,
      preview: (p) =>
        `${copy.preview(p)}. Complete its setup on the Credentials tab.`,
      installed: (p) =>
        `${copy.installed(p)}. Complete its setup on the Credentials tab.`,
    }
  }
  return { ...copy, needsSetup }
}

/** How many slots of an install still need the user before the skill works. */
export function countSlotsNeedingSetup(
  items: ReadonlyArray<ProvisionState>,
): number {
  return items.filter((item) => slotOutcomeCopy(item).needsSetup).length
}

/** The row's dot: ready is `on`; anything the user must fill in is `warning`. */
export function slotOutcomeStatus(
  item: ProvisionItem,
  phase: SlotPhase,
): RowStatus {
  const copy = slotOutcomeCopy(item)
  const params = { slot: item.slot, credentialName: item.credential_name }
  return {
    tone: copy.needsSetup ? "warning" : "on",
    label: phase === "preview" ? copy.preview(params) : copy.installed(params),
  }
}

/** The line under an install's slot list. */
export function slotOutcomeSummary(
  items: ReadonlyArray<ProvisionState>,
  phase: SlotPhase,
): string {
  const count = countSlotsNeedingSetup(items)
  if (count === 0) return "Ready to use on this agent."
  if (phase === "preview") {
    return `${count} ${plural(count, "needs", "need")} setup after install.`
  }
  return `${count} ${plural(count, "credential needs", "credentials need")} setup before the skill can use ${plural(count, "it", "them")}.`
}

/** The Addons row's dot label for `status_code === "credential_missing"`. */
export const CREDENTIAL_MISSING_LABEL =
  "Needs a credential — open the agent's Credentials tab."

/** Why an installed skill's slot is not usable (S5b). */
export const ISSUE_REASON_COPY: Record<
  IssueReason,
  { short: string; sentence: (slot: string) => string }
> = {
  not_linked: {
    short: "Nothing linked",
    sentence: (slot) => `Nothing linked for ${slot}`,
  },
  not_configured: {
    short: "Not filled in",
    sentence: (slot) => `${slot} is linked but not filled in`,
  },
  access_revoked: {
    short: "Publisher stopped sharing",
    sentence: (slot) => `The publisher stopped sharing ${slot}`,
  },
}

/**
 * What a revision requires, as one line (catalog tile, package card fact).
 * `null` when it requires nothing — the caller renders no line at all. A
 * `template` slot counts as *from you*: you add the secret.
 */
export function requirementsSummary(
  requirements:
    | ReadonlyArray<Pick<SkillCredentialRequirementPublic, "provided_by">>
    | null
    | undefined,
): string | null {
  const total = requirements?.length ?? 0
  if (!requirements || total === 0) return null
  const fromYou = requirements.filter(
    (req) => slotProvidedBy(req.provided_by) !== "publisher",
  ).length
  const noun = `${total} ${plural(total, "credential", "credentials")}`
  if (fromYou === 0) return `${noun}, provided by the publisher`
  if (fromYou === total) {
    return `${noun}, you provide ${plural(total, "it", "them")}`
  }
  return `${noun}, ${fromYou} from you`
}

/*
 * The two halves of a credential's disable-sharing / delete impact (S6), one
 * per source. Callers show each only when that source has active installs —
 * a published package nobody installed loses nothing — and each drops its
 * consequence clause at zero installs, so neither can say "those installs"
 * about none.
 */

/** The bundle half — the sentences the confirms carried before skills existed. */
export function bundleImpactSentence(
  bundles: number,
  installs: number,
  action: ImpactAction,
): string {
  const bundleNoun = plural(bundles, "bundle", "bundles")
  const withInstalls =
    installs > 0
      ? ` with ${installs} active ${plural(installs, "install", "installs")}`
      : ""
  if (action === "disable") {
    const tail =
      installs > 0
        ? " Disabling sharing will leave those installs without their publisher-provided credentials."
        : ""
    return `This credential is provided by the publisher in ${bundles} published ${bundleNoun}${withInstalls}.${tail}`
  }
  const tail =
    installs > 0
      ? " Deleting it will break those installs — their owners will be told the publisher-provided credentials are unavailable. Consider rotating the credential value instead, or remove it from each bundle first."
      : ""
  return `It is provided by the publisher in published ${bundleNoun}${withInstalls}.${tail}`
}

/** The skill half. */
export function skillImpactSentence(
  skills: number,
  installs: number,
  action: ImpactAction,
): string {
  const where = `Provided by the publisher in ${skills} published ${plural(skills, "skill", "skills")}`
  if (installs === 0) return `${where}.`
  const tail =
    action === "disable"
      ? "Disabling sharing leaves those installs without it."
      : "Deleting it leaves those installs without it."
  return `${where} with ${installs} active ${plural(installs, "install", "installs")}. ${tail}`
}
