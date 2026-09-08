import { Link as RouterLink, useRouterState } from "@tanstack/react-router"
import { Bot, GraduationCap } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * The two sections of the Catalog destination: agent bundles and skills.
 *
 * The sidebar has **one** Catalog entry and always will — two entries for one
 * destination is how a nav grows a row per route. So the second section is
 * reachable from inside the page, through the same pill vocabulary
 * `CatalogFilters` already uses on both routes: identical shape, one row above
 * the filters, so the eye reads "which catalog" then "which subset".
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
    <div className="flex items-center gap-2">
      {SECTIONS.map((section) => {
        // "Agents" is the index route, so it can only match exactly — the
        // bundle install route lives under /catalog/agents and must not light
        // up the Skills tab, and /catalog/skills/<id> must not light up Agents.
        const active = section.exact
          ? pathname === section.to || pathname.startsWith("/catalog/agents")
          : pathname.startsWith(section.to)
        const Icon = section.icon
        return (
          <Button
            key={section.to}
            asChild
            variant={active ? "default" : "outline"}
            size="sm"
            className={cn("gap-1.5", !active && "text-muted-foreground")}
          >
            <RouterLink
              to={section.to}
              aria-current={active ? "page" : undefined}
            >
              <Icon className="h-3.5 w-3.5" />
              {section.label}
            </RouterLink>
          </Button>
        )
      })}
    </div>
  )
}
