import { createFileRoute, Outlet } from "@tanstack/react-router"

/**
 * Layout route for `/login` and its children (`/login/mfa`). Rendering
 * an `<Outlet>` keeps the child routes (signup-style login form at
 * `/login`, the MFA challenge at `/login/mfa`) addressable as siblings
 * without needing a flat-route naming scheme.
 */
export const Route = createFileRoute("/login")({
  component: () => <Outlet />,
})
