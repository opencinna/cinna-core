import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import {
  AppWindow,
  Check,
  ChevronDown,
  CircleX,
  FileText,
  Gauge,
  LayoutDashboard,
  LayoutGrid,
  List,
  type LucideIcon,
  Search,
  Table,
  TriangleAlert,
} from "lucide-react"
import { useMemo, useState } from "react"

import { AgentDashboardsService, type DashboardComponentPublic } from "@/client"
import { RowFlag, RowInfo } from "@/components/Common/ListRow"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import {
  type ComponentView,
  componentTitle,
  readSize,
  readView,
  SIZE_GRID,
} from "./componentSpec"
import { componentErrorMessage, readComponentError } from "./tileConfig"

const SEARCH_FROM = 8

export const VIEW_GLYPHS: Record<
  ComponentView,
  { icon: LucideIcon; label: string }
> = {
  markdown: { icon: FileText, label: "Markdown view" },
  table: { icon: Table, label: "Table view" },
  list: { icon: List, label: "List view" },
  metrics: { icon: Gauge, label: "Metrics view" },
  html: { icon: AppWindow, label: "Web page view" },
}

export function componentKey(
  c: Pick<DashboardComponentPublic, "name" | "skill">,
) {
  return `${c.skill || ""}/${c.name}`
}

export function componentsQueryKey(agentId: string) {
  return ["agentDashboardComponents", agentId] as const
}

function labelOf(component: DashboardComponentPublic) {
  return componentTitle(component) || component.name
}

function ViewGlyph({ component }: { component: DashboardComponentPublic }) {
  const view = readView(component)
  const glyph = view
    ? VIEW_GLYPHS[view]
    : { icon: LayoutGrid, label: "Unknown view" }
  return <RowFlag icon={glyph.icon} label={glyph.label} />
}

interface Props {
  agentId: string
  dashboardId: string
  value: DashboardComponentPublic | null
  onChange: (component: DashboardComponentPublic) => void
  disabled?: boolean
}

/**
 * S2's Component field: a trigger + `Popover` over the agent's components
 * (`GET /agents/{id}/dashboards`). A component with errors stays listed,
 * not addable, with its first error as the reason; one already on this
 * dashboard stays addable (two tiles with different params are legitimate).
 */
export function ComponentPicker({
  agentId,
  dashboardId,
  value,
  onChange,
  disabled,
}: Props) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState("")

  const query = useQuery({
    queryKey: componentsQueryKey(agentId),
    queryFn: () =>
      AgentDashboardsService.listAgentDashboardComponents({ agentId }),
    enabled: !!agentId,
    retry: false,
  })
  const components = query.data?.components ?? []
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return components
    return components.filter(
      (c) =>
        labelOf(c).toLowerCase().includes(needle) ||
        c.name.toLowerCase().includes(needle),
    )
  }, [components, search])

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setSearch("")
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className={cn(
            "flex min-h-9 w-full items-center justify-between gap-2 rounded-md border border-input",
            "bg-transparent px-3 py-1.5 text-sm shadow-xs outline-none transition-[color,box-shadow]",
            "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
            "disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30 dark:hover:bg-input/50",
          )}
        >
          {value ? (
            <span className="flex min-w-0 items-center gap-2">
              <ViewGlyph component={value} />
              <span className="truncate">{labelOf(value)}</span>
            </span>
          ) : (
            <span className="text-muted-foreground">Select a component</span>
          )}
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-(--radix-popover-trigger-width) p-2"
      >
        <PickerBody
          query={query}
          agentId={agentId}
          components={components}
          filtered={filtered}
          search={search}
          onSearch={setSearch}
          dashboardId={dashboardId}
          value={value}
          onPick={(component) => {
            onChange(component)
            setOpen(false)
          }}
        />
      </PopoverContent>
    </Popover>
  )
}

function PickerBody({
  query,
  agentId,
  components,
  filtered,
  search,
  onSearch,
  dashboardId,
  value,
  onPick,
}: {
  query: {
    isLoading: boolean
    isError: boolean
    error: unknown
    refetch: () => unknown
  }
  agentId: string
  components: DashboardComponentPublic[]
  filtered: DashboardComponentPublic[]
  search: string
  onSearch: (text: string) => void
  dashboardId: string
  value: DashboardComponentPublic | null
  onPick: (component: DashboardComponentPublic) => void
}) {
  if (query.isLoading) {
    return (
      <div className="space-y-2 p-1">
        <Skeleton className="h-6 w-full" />
        <Skeleton className="h-6 w-full" />
        <Skeleton className="h-6 w-2/3" />
      </div>
    )
  }
  if (query.isError) {
    const asleep = readComponentError(query.error)?.code === "env_unavailable"
    return (
      <QueryErrorAlert
        compact
        error={{
          message: asleep
            ? "The agent's environment is not running. Start it to list its components."
            : componentErrorMessage(query.error, "The request failed."),
        }}
        fallback="Couldn't load this agent's components."
        onRetry={() => void query.refetch()}
      />
    )
  }
  if (components.length === 0) {
    return (
      <p className="p-2 text-sm text-muted-foreground">
        This agent has no dashboard components yet. Ask it to build one in{" "}
        <Link
          to="/agent/$agentId"
          params={{ agentId }}
          className="text-foreground underline-offset-2 hover:underline"
        >
          a chat
        </Link>
        .
      </p>
    )
  }

  return (
    <div className="space-y-2">
      {components.length > SEARCH_FROM && (
        <div className="relative">
          <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            placeholder="Search components"
            className="h-8 pl-7 text-sm"
          />
        </div>
      )}
      <div
        role="radiogroup"
        aria-label="Components"
        className="max-h-[280px] space-y-0.5 overflow-y-auto pr-2"
      >
        {filtered.length === 0 && (
          <p className="p-2 text-sm text-muted-foreground">
            No components match "{search}".
          </p>
        )}
        {filtered.map((component) => (
          <ComponentOption
            key={componentKey(component)}
            component={component}
            dashboardId={dashboardId}
            checked={!!value && componentKey(value) === componentKey(component)}
            onPick={onPick}
          />
        ))}
      </div>
    </div>
  )
}

function firstMessageOf(messages: string[]) {
  if (messages.length === 0) return ""
  return messages.length === 1
    ? messages[0]
    : `${messages[0]} and ${messages.length - 1} more`
}

function ComponentOption({
  component,
  dashboardId,
  checked,
  onPick,
}: {
  component: DashboardComponentPublic
  dashboardId: string
  checked: boolean
  onPick: (component: DashboardComponentPublic) => void
}) {
  const findings = component.findings ?? []
  const errors = findings
    .filter((f) => f.severity === "error")
    .map((f) => f.message)
  const warnings = findings
    .filter((f) => f.severity === "warning")
    .map((f) => f.message)
  const blocked = component.has_errors || errors.length > 0
  const placedHere = (component.placements ?? []).some(
    (p) => p.dashboard_id === dashboardId,
  )
  const size = readSize(component)

  return (
    <Button
      type="button"
      variant="ghost"
      role="radio"
      aria-checked={checked}
      aria-disabled={blocked || undefined}
      onClick={() => {
        if (!blocked) onPick(component)
      }}
      className={cn(
        "h-8 w-full justify-start gap-2 px-2 font-normal",
        blocked &&
          "cursor-not-allowed text-muted-foreground hover:bg-transparent",
      )}
    >
      <ViewGlyph component={component} />
      <span className="min-w-0 flex-1 truncate text-left">
        {labelOf(component)}
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {blocked && (
          <RowFlag
            icon={CircleX}
            tone="error"
            label={`Not addable: ${firstMessageOf(errors) || "the component has problems."}`}
          />
        )}
        {!blocked && warnings.length > 0 && (
          <RowFlag
            icon={TriangleAlert}
            tone="warning"
            label={firstMessageOf(warnings)}
          />
        )}
        {placedHere && (
          <RowFlag icon={LayoutDashboard} label="Already on this dashboard" />
        )}
        {checked && <Check className="h-3.5 w-3.5 text-foreground" />}
        <RowInfo
          facts={[
            component.skill && `From skill ${component.skill}`,
            size && `Size: ${SIZE_GRID[size].w}×${SIZE_GRID[size].h}`,
          ]}
        />
      </span>
    </Button>
  )
}
