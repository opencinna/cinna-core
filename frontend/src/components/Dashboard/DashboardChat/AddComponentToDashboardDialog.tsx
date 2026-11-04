import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Check, ChevronDown, LayoutDashboard, Search } from "lucide-react"
import { useMemo, useState } from "react"

import {
  type ComponentRefPlacement,
  DashboardsService,
  type UserDashboardPublic,
} from "@/client"
import { RowFlag } from "@/components/Common/ListRow"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  type ComponentSize,
  readSize,
  SIZE_GRID,
} from "@/components/Dashboard/AgentComponents/componentSpec"
import { componentErrorMessage } from "@/components/Dashboard/AgentComponents/tileConfig"
import { useAgentComponent } from "@/components/Dashboard/AgentComponents/useAgentComponent"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"

type SizeChoice = "small" | "wide" | "large"

/** The three tile sizes, on the same 12-column grid as the component's own size hint. */
const SIZES: Array<{
  value: SizeChoice
  label: string
  grid: { w: number; h: number }
}> = [
  { value: "small", label: "Small", grid: SIZE_GRID.s },
  { value: "wide", label: "Wide", grid: SIZE_GRID.m },
  { value: "large", label: "Large", grid: SIZE_GRID.l },
]

const NEAREST: Record<ComponentSize, SizeChoice> = {
  s: "small",
  tall: "small",
  m: "wide",
  wide: "wide",
  l: "large",
}

const SEARCH_FROM = 8

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  agentId: string
  name: string
  skill: string | null
  title: string
  placements: ComponentRefPlacement[]
}

/**
 * Add a referenced component to one of the owner's dashboards (UI spec S6b):
 * Dashboard · Size, creating an `agent_component` block.
 */
export default function AddComponentToDashboardDialog({
  open,
  onOpenChange,
  agentId,
  name,
  skill,
  title,
  placements,
}: Props) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [dashboard, setDashboard] = useState<UserDashboardPublic | null>(null)
  const componentQuery = useAgentComponent(agentId, name, skill)
  const hint = readSize(componentQuery.data)
  const [size, setSize] = useState<SizeChoice | null>(null)
  const effectiveSize = size ?? (hint ? NEAREST[hint] : "small")

  const dashboardsQuery = useQuery({
    queryKey: ["userDashboards"],
    queryFn: () => DashboardsService.listDashboards(),
  })

  const addMutation = useMutation({
    mutationFn: (target: UserDashboardPublic) => {
      const grid =
        SIZES.find((s) => s.value === effectiveSize)?.grid ?? SIZE_GRID.s
      return DashboardsService.addBlock({
        dashboardId: target.id,
        requestBody: {
          agent_id: agentId,
          view_type: "agent_component",
          title: null,
          show_border: true,
          grid_x: 0,
          grid_y: 0,
          grid_w: grid.w,
          grid_h: grid.h,
          config: { component: name, skill, params: {}, refresh: {} },
        },
      })
    },
    onSuccess: (_block, target) => {
      showSuccessToast(`Added to ${target.name}.`)
      queryClient.invalidateQueries({ queryKey: ["userDashboard", target.id] })
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      queryClient.invalidateQueries({ queryKey: ["componentRef"] })
      onOpenChange(false)
    },
    onError: (error) =>
      showErrorToast(
        componentErrorMessage(error, "Couldn't add the component."),
      ),
  })

  const placedOn = useMemo(
    () => new Set(placements.map((p) => p.dashboard_id)),
    [placements],
  )
  const dashboards = dashboardsQuery.data ?? []
  const noDashboards = dashboardsQuery.isSuccess && dashboards.length === 0

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!addMutation.isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">
            Add {title} to a dashboard
          </DialogTitle>
          <DialogDescription>
            It shows as a tile you can refresh and ask about.
          </DialogDescription>
        </DialogHeader>

        {noDashboards ? (
          <p className="text-sm text-muted-foreground">
            You have no dashboards yet.{" "}
            <Link
              to="/dashboards"
              className="text-foreground underline-offset-2 hover:underline"
            >
              Create one
            </Link>
          </p>
        ) : (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label id="add-component-dashboard">Dashboard</Label>
              <DashboardPicker
                query={dashboardsQuery}
                dashboards={dashboards}
                placedOn={placedOn}
                value={dashboard}
                onChange={setDashboard}
                disabled={addMutation.isPending}
              />
            </div>
            <div className="space-y-1.5">
              <Label id="add-component-size">Size</Label>
              <div
                role="radiogroup"
                aria-labelledby="add-component-size"
                className="flex flex-wrap gap-2"
              >
                {SIZES.map((option) => {
                  const checked = option.value === effectiveSize
                  return (
                    // biome-ignore lint/a11y/useSemanticElements: a pill; §2 Choice-control ARIA asks for role="radio" on hand-rolled one-of-n options.
                    <button
                      key={option.value}
                      type="button"
                      role="radio"
                      aria-checked={checked}
                      disabled={addMutation.isPending}
                      onClick={() => setSize(option.value)}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors",
                        "outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
                        checked
                          ? "border-primary bg-primary text-primary-foreground"
                          : "bg-background hover:bg-muted",
                      )}
                    >
                      {option.label}
                      <span
                        className={cn(
                          "tabular-nums text-xs",
                          checked
                            ? "text-primary-foreground/80"
                            : "text-muted-foreground",
                        )}
                      >
                        {option.grid.w}×{option.grid.h}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={addMutation.isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            onClick={() => dashboard && addMutation.mutate(dashboard)}
            loading={addMutation.isPending}
            disabled={!dashboard || noDashboards}
          >
            {addMutation.isPending ? "Adding…" : "Add to dashboard"}
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DashboardPicker({
  query,
  dashboards,
  placedOn,
  value,
  onChange,
  disabled,
}: {
  query: {
    isLoading: boolean
    isError: boolean
    error: unknown
    refetch: () => unknown
  }
  dashboards: UserDashboardPublic[]
  placedOn: ReadonlySet<string>
  value: UserDashboardPublic | null
  onChange: (dashboard: UserDashboardPublic) => void
  disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState("")
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return needle
      ? dashboards.filter((d) => d.name.toLowerCase().includes(needle))
      : dashboards
  }, [dashboards, search])

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
          aria-labelledby="add-component-dashboard"
          className={cn(
            "flex min-h-9 w-full items-center justify-between gap-2 rounded-md border border-input",
            "bg-transparent px-3 py-1.5 text-sm shadow-xs outline-none transition-[color,box-shadow]",
            "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
            "disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30 dark:hover:bg-input/50",
          )}
        >
          {value ? (
            <span className="flex min-w-0 items-center gap-2">
              <LayoutDashboard className="h-4 w-4 shrink-0 text-muted-foreground" />
              <span className="truncate">{value.name}</span>
            </span>
          ) : (
            <span className="text-muted-foreground">Select a dashboard</span>
          )}
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-(--radix-popover-trigger-width) p-2"
      >
        {query.isLoading ? (
          <div className="space-y-2 p-1">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-2/3" />
          </div>
        ) : query.isError ? (
          <QueryErrorAlert
            compact
            error={query.error}
            fallback="Couldn't load your dashboards."
            onRetry={() => void query.refetch()}
          />
        ) : (
          <div className="space-y-2">
            {dashboards.length > SEARCH_FROM && (
              <div className="relative">
                <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search dashboards"
                  className="h-8 pl-7 text-sm"
                />
              </div>
            )}
            <div
              role="radiogroup"
              aria-label="Dashboards"
              className="max-h-[280px] space-y-0.5 overflow-y-auto pr-2"
            >
              {filtered.length === 0 && (
                <p className="p-2 text-sm text-muted-foreground">
                  No dashboards match "{search}".
                </p>
              )}
              {filtered.map((d) => {
                const checked = value?.id === d.id
                return (
                  // biome-ignore lint/a11y/useSemanticElements: one-of-n option with a flag beside its name; §2 Choice-control ARIA.
                  <button
                    key={d.id}
                    type="button"
                    role="radio"
                    aria-checked={checked}
                    onClick={() => {
                      onChange(d)
                      setOpen(false)
                    }}
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-muted focus-visible:bg-muted"
                  >
                    <span className="min-w-0 flex-1 truncate">{d.name}</span>
                    {placedOn.has(d.id) && (
                      <RowFlag icon={LayoutDashboard} label="Already here" />
                    )}
                    <Check
                      className={cn(
                        "h-4 w-4 shrink-0",
                        checked ? "opacity-100" : "opacity-0",
                      )}
                    />
                  </button>
                )
              })}
            </div>
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}
