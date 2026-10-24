import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import {
  Bot,
  Braces,
  Cpu,
  GraduationCap,
  type LucideIcon,
  Plug,
  SquareSlash,
  Webhook,
} from "lucide-react"

import type {
  LLMPluginMarketplacePluginPublic,
  PluginComponents,
} from "@/client"
import { StatusDot } from "@/components/Common/StatusDot"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  mcpTransportLabel,
  plural,
  snapshotReasonSentence,
  snapshotStatusMeta,
  unsupportedReasonSentence,
} from "@/utils/marketplace"

type Entry = LLMPluginMarketplacePluginPublic

/** How many names a component tooltip lists before "and n more". */
const TOOLTIP_NAMES = 5

function namesLine(names: string[]): string {
  const shown = names.slice(0, TOOLTIP_NAMES).join(", ")
  const rest = names.length - TOOLTIP_NAMES
  return rest > 0 ? `${shown} and ${rest} more` : shown
}

/** One glyph + count, muted, with its kind in words on hover. */
function ComponentCount({
  icon: Icon,
  count,
  label,
  detail,
}: {
  icon: LucideIcon
  count?: number
  label: string
  detail?: string[]
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Icon className="h-3.5 w-3.5" />
          {count !== undefined && <span>{count}</span>}
          <span className="sr-only">{label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs space-y-0.5 text-xs">
        <p>{label}</p>
        {detail?.map((line, index) => (
          // Keyed by position: two MCP servers can print the same line, and
          // the list is rebuilt whole on every render.
          <p key={index} className="break-words">
            {line}
          </p>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * What the entry ships, one muted glyph + count per kind that is present.
 *
 * `components == null` (pending, failed) renders nothing: the Snapshot column
 * beside it says why, and a row of zeros would assert a parse that never ran.
 */
function ComponentsCell({
  components,
  runsCode,
}: {
  components: PluginComponents | null | undefined
  runsCode: boolean
}) {
  if (!components) return null
  const skills = components.skills ?? []
  const commands = components.commands ?? []
  const agents = components.agents ?? []
  const mcp = components.mcp_servers ?? []
  const events = components.hooks?.events ?? []
  const lsp = components.lsp_servers ?? []

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      {skills.length > 0 && (
        <ComponentCount
          icon={GraduationCap}
          count={skills.length}
          label={plural(skills.length, "skill")}
          detail={[namesLine(skills.map((s) => s.name))]}
        />
      )}
      {commands.length > 0 && (
        <ComponentCount
          icon={SquareSlash}
          count={commands.length}
          label={plural(commands.length, "command")}
          detail={[namesLine(commands)]}
        />
      )}
      {agents.length > 0 && (
        <ComponentCount
          icon={Bot}
          count={agents.length}
          label={plural(agents.length, "agent")}
          detail={[namesLine(agents)]}
        />
      )}
      {mcp.length > 0 && (
        <ComponentCount
          icon={Plug}
          count={mcp.length}
          label={plural(mcp.length, "MCP server")}
          detail={mcp.map((server) =>
            server.runtime
              ? `${server.name} — ${mcpTransportLabel(server.transport)} (${server.runtime})`
              : `${server.name} — ${mcpTransportLabel(server.transport)}`,
          )}
        />
      )}
      {events.length > 0 && (
        <ComponentCount
          icon={Webhook}
          count={events.length}
          label={`Hooks on ${plural(events.length, "event")}`}
          detail={[namesLine(events)]}
        />
      )}
      {lsp.length > 0 && (
        <ComponentCount
          icon={Braces}
          count={lsp.length}
          label={plural(lsp.length, "LSP server")}
          detail={[namesLine(lsp)]}
        />
      )}
      {runsCode && (
        <ComponentCount icon={Cpu} label="Runs code on the user's machine" />
      )}
    </div>
  )
}

function SnapshotCell({ entry }: { entry: Entry }) {
  const meta = snapshotStatusMeta(entry.snapshot_status)
  if (!meta) return null
  return (
    <StatusDot
      tone={meta.tone}
      label={meta.label}
      reason={
        meta.status === "failed"
          ? snapshotReasonSentence(entry.snapshot_reason)
          : null
      }
    />
  )
}

/**
 * Can the cloud platform install the entry (the column was "Supported",
 * renamed so it cannot be read as the snapshot). Optional on the wire, so an
 * entry synced before the flag existed reads as supported.
 */
function CloudInstallCell({ entry }: { entry: Entry }) {
  if (entry.supported !== false) {
    return <StatusDot tone="on" label="Supported" />
  }
  return (
    <StatusDot
      tone="error"
      label="Not installable"
      reason={unsupportedReasonSentence(entry.unsupported_reason)}
    />
  )
}

/**
 * The marketplace entries list (S1). Column order is final from K1: K5
 * inserts select first, Access after Snapshot and ⋯ last.
 */
export function getMarketplaceEntryColumns(): ColumnDef<Entry>[] {
  return [
    {
      id: "name",
      header: "Name",
      // `max-w-0` lets the cell shrink under `table-fixed` so the
      // description truncates instead of widening the table.
      meta: { className: "max-w-0" },
      cell: ({ row }) => {
        const entry = row.original
        return (
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <Link
                to="/admin/marketplace/plugin/$pluginId"
                params={{ pluginId: entry.id }}
                className="truncate text-sm font-medium hover:underline"
              >
                {entry.name}
              </Link>
              {entry.version && (
                <Badge variant="secondary" className="h-5 shrink-0">
                  v{entry.version}
                </Badge>
              )}
            </div>
            {entry.description && (
              <p className="truncate text-xs text-muted-foreground">
                {entry.description}
              </p>
            )}
          </div>
        )
      },
    },
    {
      id: "components",
      header: "Components",
      meta: { className: "w-40" },
      cell: ({ row }) => (
        <ComponentsCell
          components={row.original.components}
          runsCode={row.original.runs_code === true}
        />
      ),
    },
    {
      id: "snapshot",
      header: "Snapshot",
      meta: { className: "w-28" },
      cell: ({ row }) => <SnapshotCell entry={row.original} />,
    },
    {
      id: "cloud",
      header: "Cloud install",
      meta: { className: "hidden xl:table-cell w-40" },
      cell: ({ row }) => <CloudInstallCell entry={row.original} />,
    },
    {
      id: "author",
      header: "Author",
      meta: { className: "hidden xl:table-cell w-40" },
      cell: ({ row }) => (
        <span className="block truncate text-sm text-muted-foreground">
          {row.original.author_name || ""}
        </span>
      ),
    },
  ]
}
