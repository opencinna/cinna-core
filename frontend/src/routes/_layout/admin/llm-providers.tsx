import { createFileRoute, redirect } from "@tanstack/react-router"

/**
 * Redirect stub for the page's former path.
 *
 * The admin surface moved from "LLM Providers" to "AI Credentials" — a UI
 * rename only; the backend prefix `/admin/llm-providers`, its OpenAPI tag and
 * the generated `AdminLlmProvidersService` are unchanged. This keeps bookmarks
 * and older documentation working.
 *
 * `beforeLoad` only, and deliberately no auth guards of its own: the target
 * route runs them, and duplicating them here would be a second place to keep
 * them in step.
 */
export const Route = createFileRoute("/_layout/admin/llm-providers")({
  beforeLoad: () => {
    throw redirect({ to: "/admin/ai-credentials" })
  },
})
