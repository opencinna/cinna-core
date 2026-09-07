/**
 * The list of native app clients (Cinna Desktop / Cinna Mobile) signed in to
 * the current account.
 *
 * The key lives in a neutral module rather than on the card or the row, so
 * anything that invalidates the list — the row's revoke today, a desktop
 * connect flow or a "signed out elsewhere" handler tomorrow — depends on this
 * constant instead of on a component.
 */
export const APP_SESSIONS_QUERY_KEY = ["desktop-clients"] as const
