/**
 * The Context tab of a session-kind improvement request: the runtime context
 * frozen at submission (which install, which revision, which environment and
 * model, whether the prompts had been edited). It used to be a nested "Context
 * details" `Dialog` opened from inside the request's dialog (R8); it is now a
 * tab of that dialog, with the same rows.
 */
import type { ImprovementRequestDetailPublic } from "@/client"

/**
 * Human copy for `context.memory.unavailable_reason`
 * (`session_snapshot_service.MEMORY_REASON_*`). "Not captured" alone would be
 * read as "this install has no memory", which is a different conclusion from
 * "the container was off" — and only one of them means the recipient can stop
 * wondering about it.
 */
const MEMORY_REASON_COPY: Record<string, string> = {
  declined_by_requester: "not shared — the requester opted out",
  no_environment: "not captured — the install had no environment",
  env_not_running: "not captured — the environment was stopped",
  read_failed: "not captured — the container could not be read",
  empty: "none — this install had no memory notes",
}

type ContextBlock = Record<string, unknown>

/** One block of the frozen `context` JSON, or an empty object when absent. */
const block = (context: ContextBlock | undefined, name: string): ContextBlock =>
  (context?.[name] as ContextBlock | undefined) ?? {}

/** Render any frozen-context scalar as display text. */
const asText = (value: unknown): string | null => {
  if (value === null || value === undefined || value === "") return null
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (typeof value === "number") return String(value)
  if (typeof value === "string") return value
  return null
}

/**
 * Label one bundle revision.
 *
 * A revision with no `version` is *unversioned* (every git-origin revision is,
 * today), which is a fact about it rather than missing data — printing a bare
 * dash there reads as "we don't know". The origin is appended when it is not a
 * publish, because that is what explains an install sitting above the latest
 * published revision.
 */
const revisionLabel = (
  version: unknown,
  revisionNumber: unknown,
  origin?: unknown,
): string | null => {
  const versionText = asText(version)
  const numberText = asText(revisionNumber)
  if (!versionText && !numberText) return null
  const label = versionText
    ? numberText
      ? `${versionText} (revision ${numberText})`
      : versionText
    : `revision ${numberText} (unversioned)`
  const originText = asText(origin)
  return originText && originText !== "publish"
    ? `${label} · from ${originText}`
    : label
}

const plural = (count: number, one: string, many: string) =>
  `${count} ${count === 1 ? one : many}`

interface DetailRow {
  label: string
  value: string | null
}

function contextRows(context: ContextBlock | undefined): DetailRow[] {
  const agentBlock = block(context, "agent")
  const envBlock = block(context, "environment")
  const sdkBlock = block(context, "sdk")
  const plugins = Array.isArray(context?.plugins) ? context.plugins : []
  const promptsBlock = block(context, "prompts")
  const memoryBlock = block(context, "memory")

  // `context.prompts.diverged` is deliberately tri-state: `null` means there
  // was no installed revision to diff against, which is not the same answer as
  // "no". Rendering it as "no" would tell a publisher their text is intact
  // when nothing was ever compared. It covers the published prompt documents
  // only — the router trigger is routing metadata the platform writes by itself
  // and the install's owner may set, so counting it would flag nearly every
  // consumer install for an edit no person made.
  const divergedFields = Array.isArray(promptsBlock.diverged_fields)
    ? (promptsBlock.diverged_fields as string[])
    : []
  const promptsDiverged =
    typeof promptsBlock.diverged === "boolean"
      ? promptsBlock.diverged
        ? `yes — ${divergedFields.length > 0 ? divergedFields.join(", ") : "the install's prompts"} differ from the published revision`
        : "no — matches the published revision"
      : "no baseline to compare against"

  // Revision numbers are shared between published and git-origin revisions and
  // only a publish moves the bundle's "latest published" pointer, so an install
  // can legitimately sit above it. Both sides are labelled rather than printed
  // as bare version strings.
  const installedLabel = revisionLabel(
    agentBlock.installed_version,
    agentBlock.installed_revision_number,
    agentBlock.installed_revision_origin,
  )
  const publishedLabel = revisionLabel(
    agentBlock.latest_published_version || agentBlock.latest_version,
    agentBlock.latest_published_revision_number ||
      agentBlock.latest_revision_number,
  )

  const memorySummary = memoryBlock.available
    ? `${plural(Number(memoryBlock.file_count) || 0, "file", "files")} captured`
    : (MEMORY_REASON_COPY[String(memoryBlock.unavailable_reason ?? "")] ??
      "not captured")

  const rows: DetailRow[] = [
    // `context.agent` is the *source* install — the consumer's copy — so a
    // publisher must not read this as their own agent's name.
    { label: "Agent (as installed)", value: asText(agentBlock.name) },
    { label: "Bundle", value: asText(agentBlock.bundle_id) },
    { label: "Installed", value: installedLabel },
    { label: "Latest published", value: publishedLabel },
    { label: "Pending update", value: asText(agentBlock.update_pending) },
    { label: "Session mode", value: asText(sdkBlock.session_mode) },
    { label: "SDK engine", value: asText(sdkBlock.effective_engine) },
    { label: "Effective model", value: asText(sdkBlock.effective_model) },
    { label: "Environment", value: asText(envBlock.env_name) },
    { label: "Environment version", value: asText(envBlock.env_version) },
    { label: "Environment status", value: asText(envBlock.status_at_capture) },
    {
      label: "Image freshness",
      value:
        typeof envBlock.image_stale === "boolean"
          ? envBlock.image_stale
            ? "Stale"
            : "Current"
          : null,
    },
    { label: "Critical state", value: asText(envBlock.critical_state) },
    { label: "Plugins", value: plural(plugins.length, "plugin", "plugins") },
    // Only for requests captured after prompt/memory capture existed — an
    // older row has no `prompts` block, and an empty row would read as "no
    // prompts".
    ...(Object.keys(promptsBlock).length > 0
      ? [
          { label: "Prompt edits on the install", value: promptsDiverged },
          { label: "Personal memory", value: memorySummary },
        ]
      : []),
  ]
  return rows.filter((row) => row.value !== null)
}

export function ImprovementRequestSessionContext({
  request,
}: {
  request: ImprovementRequestDetailPublic
}) {
  const rows = contextRows(request.context)
  return (
    <>
      <p className="text-sm text-muted-foreground">
        Frozen when the request was submitted — not a live read of the install.
      </p>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No runtime context was captured with this request.
        </p>
      ) : (
        <dl className="divide-y rounded-md border text-sm">
          {rows.map((row) => (
            <div
              key={row.label}
              className="flex items-start justify-between gap-3 px-3 py-2"
            >
              <dt className="shrink-0 text-muted-foreground">{row.label}</dt>
              <dd className="min-w-0 break-words text-right font-medium">
                {row.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </>
  )
}
