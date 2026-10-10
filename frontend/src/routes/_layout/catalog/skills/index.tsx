import { createFileRoute, redirect } from "@tanstack/react-router"

/**
 * The skills section folded into the unified catalog's kind control; old links
 * land on the same grid narrowed to skills.
 */
export const Route = createFileRoute("/_layout/catalog/skills/")({
  beforeLoad: () => {
    throw redirect({ to: "/catalog", search: { kind: "skill" }, replace: true })
  },
})
