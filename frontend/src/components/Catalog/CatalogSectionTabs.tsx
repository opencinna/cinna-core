import { Link as RouterLink, useRouterState } from "@tanstack/react-router"
import { Bot, GraduationCap } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * The two sections of the Catalog destination: agent bundles and skills.
 *
 * The sidebar has **one** Catalog entry and always will — two entries for one
 * destination is how a nav grows a row per route. So the second section is
 * reachable from inside the page.
 *
 * A **segmented control**: one bordered track, the current section raised out
 * of it. It used to be two `Button`s in the same pill vocabulary as the filter
 * row directly beneath, which put eight identical controls on the page — half
 * of them navigation, half of them a filter, and nothing but their labels
 * saying which was which. One track that visibly holds exactly two choices
 * cannot be mistaken for the single `Filters` button beside it.
 *
 * Rendered at the top of both catalog routes. `AppSidebar`'s `CatalogMenu`
 * matches `startsWith("/catalog")` so the sidebar item stays lit on either.
 */
const SECTIONS = [
  { to: "/catalog", label: "Agents", icon: Bot, exact: true },
  { to: "/catalog/skills", label: "Skills", icon: GraduationCap, exact: false },
] as const

export function CatalogSectionTabs() {
  const pathname = useRouterState({ select: (s) => s.location.pathname })

  return (
    // `nav`, not a toggle group: these are links, and a screen-reader user gets
    // "navigation" rather than a set of buttons that happen to change the page.
    <nav
      aria-label="Catalog sections"
      className="inline-flex items-center gap-1 rounded-lg border bg-muted/50 p-1"
    >
      {SECTIONS.map((section) => {
        // "Agents" is the index route, so it can only match exactly — the
        // bundle install route lives under /catalog/agents and must not light
        // up the Skills tab, and /catalog/skills/<id> must not light up Agents.
        const active = section.exact
          ? pathname === section.to || pathname.startsWith("/catalog/agents")
          : pathname.startsWith(section.to)
        const Icon = section.icon
        return (
          <RouterLink
            key={section.to}
            to={section.to}
            aria-current={active ? "page" : undefined}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium",
              "transition-colors outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
              // The current section is the brand colour, not a raised
              // `bg-background` tile: on the dark skin that tile is darker than
              // the track it sits in, so the selected tab read as a hole rather
              // than as the thing that is on.
              active
                ? "bg-primary text-primary-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {section.label}
          </RouterLink>
        )
      })}
    </nav>
  )
}
