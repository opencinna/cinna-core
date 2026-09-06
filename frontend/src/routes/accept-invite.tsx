import { createFileRoute, Outlet } from "@tanstack/react-router"

/**
 * Layout route for `/accept-invite` and its child `/accept-invite/done`.
 *
 * Same shape, and same reason, as `login.tsx`: a file-based parent has to
 * render an `<Outlet>` for its children to be addressable, so the accept page
 * itself lives in `accept-invite/index.tsx`.
 *
 * Deliberately unguarded, like `desktop.tsx` and unlike `desktop-auth/consent`
 * — the whole point of this page is that the visitor has no account session
 * yet. The child `done` route adds its own `ensureSessionValid`, because by
 * then they do.
 */
export const Route = createFileRoute("/accept-invite")({
  component: () => <Outlet />,
})
