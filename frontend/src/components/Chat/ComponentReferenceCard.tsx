import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Copy, LayoutGrid } from "lucide-react"
import { Fragment, lazy, Suspense, useState } from "react"

import { AgentsService, type ComponentRefResolved } from "@/client"
import { VIEW_GLYPHS } from "@/components/Dashboard/AgentComponents/ComponentPicker"
import type { ComponentView } from "@/components/Dashboard/AgentComponents/componentSpec"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { parseComponentRef, toBundleRef } from "@/utils/componentRefs"
import { resolveComponentRef } from "./componentRefBatcher"
import { useComponentRefChat } from "./componentRefContext"

// The dialogs pull in the whole tile host; a chat that only shows cards does
// not pay for it until Preview or Add is clicked.
const ComponentPreviewDialog = lazy(
  () => import("@/components/Dashboard/DashboardChat/ComponentPreviewDialog"),
)
const AddComponentToDashboardDialog = lazy(
  () =>
    import(
      "@/components/Dashboard/DashboardChat/AddComponentToDashboardDialog"
    ),
)

const UNRESOLVED: ReadonlyArray<ComponentRefResolved["status"]> = [
  "invalid",
  "gone",
  "ambiguous",
  "not_installed",
]

export function componentRefQueryKey(ref: string, sessionId: string | null) {
  return ["componentRef", ref, sessionId] as const
}

function asView(view: string | null | undefined): ComponentView | null {
  return view && view in VIEW_GLYPHS ? (view as ComponentView) : null
}

/**
 * A component reference in a chat message (UI spec S6): what an agent built,
 * and the way to put it on a dashboard. Resolving it runs nothing and wakes
 * nothing (plan §5.4, D8).
 */
export function ComponentReferenceCard({ reference }: { reference: string }) {
  const { sessionId = null } = useComponentRefChat()
  const query = useQuery({
    queryKey: componentRefQueryKey(reference, sessionId),
    queryFn: () => resolveComponentRef(reference, sessionId),
    staleTime: 60_000,
    retry: false,
  })

  if (query.isLoading) {
    return <Skeleton className="not-prose my-2 h-[108px] max-w-md" />
  }
  if (query.isError || !query.data) {
    return (
      <CardFrame>
        <p className="text-xs text-muted-foreground">
          Couldn't look up this component.{" "}
          <Button
            variant="link"
            className="h-auto p-0 text-xs"
            onClick={() => void query.refetch()}
          >
            Retry
          </Button>
        </p>
      </CardFrame>
    )
  }
  return <ResolvedCard reference={reference} result={query.data} />
}

function CardFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className="not-prose my-2 max-w-md rounded-md border bg-muted/30 px-3 py-2 text-sm">
      {children}
    </div>
  )
}

function ResolvedCard({
  reference,
  result,
}: {
  reference: string
  result: ComponentRefResolved
}) {
  const { seedComposer } = useComponentRefChat()
  const [dialog, setDialog] = useState<"preview" | "add" | null>(null)
  const parsed = parseComponentRef(reference)
  const name = result.component?.name || parsed?.name || reference
  const skill = result.component?.skill || parsed?.skill || null
  const title = result.component?.title || name
  const view = asView(result.component?.view)
  const agent = result.agent ?? null

  if (UNRESOLVED.includes(result.status) || !agent) {
    return (
      <CardFrame>
        <CopyableReference reference={reference} />
        <p className="mt-1 text-xs text-muted-foreground">
          {result.status === "not_installed" && result.catalog_name
            ? `You don't have ${result.catalog_name}.`
            : result.message || "This component can't be shown."}
        </p>
      </CardFrame>
    )
  }

  const ok = result.status === "ok"
  const errors = (result.findings ?? []).filter((f) => f.severity === "error")
  const hasErrors = errors.length > 0
  const facts = [agent.name, skill ? `from skill ${skill}` : null].filter(
    Boolean,
  )
  const asleep = result.status === "env_asleep"
  const canPreview = ok || asleep
  const Glyph = view ? VIEW_GLYPHS[view].icon : LayoutGrid

  return (
    <CardFrame>
      <div className="flex min-w-0 items-center gap-2">
        <Glyph
          className="h-4 w-4 shrink-0 text-muted-foreground"
          aria-label={view ? VIEW_GLYPHS[view].label : "Dashboard component"}
        />
        <span className="truncate font-medium">{title}</span>
      </div>
      <p className="mt-0.5 truncate text-xs text-muted-foreground">
        {facts.join(" · ")}
      </p>
      {!ok ? (
        <p className="mt-1 text-xs text-muted-foreground">
          {result.message || "This component can't be shown right now."}
        </p>
      ) : hasErrors ? (
        <p className="mt-1 break-words text-xs text-destructive">
          {errors.length === 1 ? "1 problem" : `${errors.length} problems`}:{" "}
          {errors[0].message}
          {seedComposer && (
            <>
              {" "}
              <Button
                variant="link"
                className="h-auto p-0 text-xs"
                onClick={() =>
                  seedComposer(
                    `Fix the dashboard component ${name}: ${errors
                      .map((e) => e.message)
                      .join("; ")}`,
                  )
                }
              >
                Ask to fix
              </Button>
            </>
          )}
        </p>
      ) : (
        <Placements placements={result.placements ?? []} />
      )}
      <div className="mt-2 flex items-center gap-1.5">
        {canPreview && (
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            onClick={() => setDialog("preview")}
          >
            Preview
          </Button>
        )}
        {canPreview && (
          <AddButton disabled={hasErrors} onClick={() => setDialog("add")} />
        )}
        <CopyReferenceButton reference={reference} agentId={agent.id} />
      </div>
      <Suspense fallback={null}>
        {dialog === "preview" && (
          <ComponentPreviewDialog
            open
            onOpenChange={(open) => !open && setDialog(null)}
            agent={agent}
            name={name}
            skill={skill}
            title={title}
            canAdd={canPreview && !hasErrors}
            onAdd={() => setDialog("add")}
          />
        )}
        {dialog === "add" && (
          <AddComponentToDashboardDialog
            open
            onOpenChange={(open) => !open && setDialog(null)}
            agentId={agent.id}
            name={name}
            skill={skill}
            title={title}
            placements={result.placements ?? []}
          />
        )}
      </Suspense>
    </CardFrame>
  )
}

function AddButton({
  disabled,
  onClick,
}: {
  disabled: boolean
  onClick: () => void
}) {
  const button = (
    <Button
      variant="outline"
      size="sm"
      className="h-7"
      disabled={disabled}
      onClick={onClick}
    >
      Add to dashboard…
    </Button>
  )
  if (!disabled) return button
  return (
    <Tooltip>
      {/* A disabled button gets no pointer events; the span carries the tooltip. */}
      <TooltipTrigger asChild>
        {/* biome-ignore lint/a11y/noNoninteractiveTabindex: the wrapper makes the disabled button's reason reachable by keyboard. */}
        <span tabIndex={0}>{button}</span>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Fix its problems first
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * "On Ops and Home", "On Ops, Home and 3 more"; nothing when it is on none.
 * Cinna Desktop's dashboards are not on the web: they show once, unlinked,
 * as "Cinna Desktop".
 */
function Placements({
  placements,
}: {
  placements: ComponentRefResolved["placements"]
}) {
  const all = placements ?? []
  const list: { key: string; name: string; dashboardId?: string }[] = all
    .filter((p) => p.origin !== "desktop")
    .map((p) => ({
      key: p.dashboard_id,
      name: p.name,
      dashboardId: p.dashboard_id,
    }))
  if (all.some((p) => p.origin === "desktop")) {
    list.push({ key: "desktop", name: "Cinna Desktop" })
  }
  if (list.length === 0) return null
  const shown = list.slice(0, 2)
  const more = list.length - shown.length
  return (
    <p className="mt-1 truncate text-xs text-muted-foreground">
      On{" "}
      {shown.map((p, i) => (
        <Fragment key={p.key}>
          {i > 0 && (more > 0 ? ", " : " and ")}
          {p.dashboardId ? (
            <Link
              to="/dashboards/$dashboardId"
              params={{ dashboardId: p.dashboardId }}
              className="text-foreground hover:underline"
            >
              {p.name}
            </Link>
          ) : (
            <span className="text-foreground">{p.name}</span>
          )}
        </Fragment>
      ))}
      {more > 0 && ` and ${more} more`}
    </p>
  )
}

/** Copy a reference in its bundle form when the agent has one (toasts included). */
function useCopyReference(reference: string, agentId: string | null) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  return async () => {
    let text = reference
    if (agentId) {
      try {
        const agent = await queryClient.fetchQuery({
          queryKey: ["agent", agentId],
          queryFn: () => AgentsService.readAgent({ id: agentId }),
          staleTime: 60_000,
        })
        text = toBundleRef(reference, agent.bundle_id)
      } catch {
        // The reference as written still names the component.
      }
    }
    try {
      await navigator.clipboard.writeText(text)
      showSuccessToast("Reference copied")
    } catch {
      showErrorToast("Couldn't copy the reference")
    }
  }
}

/** An unresolved reference: the visible value is its own copy control (§2). */
function CopyableReference({ reference }: { reference: string }) {
  const copy = useCopyReference(reference, null)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="block max-w-full cursor-pointer break-all text-left font-mono text-xs hover:underline"
          aria-label="Copy the reference"
          onClick={() => void copy()}
        >
          {reference}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Click to copy
      </TooltipContent>
    </Tooltip>
  )
}

function CopyReferenceButton({
  reference,
  agentId,
}: {
  reference: string
  agentId: string | null
}) {
  const copy = useCopyReference(reference, agentId)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          aria-label="Copy reference"
          onClick={() => void copy()}
        >
          <Copy className="h-3.5 w-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Copy reference
      </TooltipContent>
    </Tooltip>
  )
}
