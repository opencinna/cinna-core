# Credentials list → table — progress

Spec: `drafts/ui_spec_credentials_list_table.md` (Part A review, Part B spec). Frontend-only.

User decisions (2026-10-16, "implement as recommended"): keep type glyph; keep Agents column; labels "My Credentials · Automatic · From bundles"; no type filter; fix Automatic tab copy to match backend categorisation; fix the limit:100 truncation (FE pages through using `count`, no backend change). Out of scope: updated_at column, bundle name on shared rows.

- 2026-10-16 — design: ui-designer review ITERATE 8/10 + spec written. Done.
- 2026-10-16 — build: ui-developer, design review round 1 ITERATE (focus gates) → round 2 PASS 10/10 ×3. New credentialColumns.tsx, CredentialSectionTabs.tsx, Pending/PendingCredentials.tsx; DataTable additive props; DeleteCredential trigger optional; deleted CredentialCard/columns/CredentialActionsMenu/EditCredential/CredentialFilters. Open: no ORDER BY on GET /credentials/.
- 2026-10-16 — code review: APPROVE with W1/S1/S2 → fixed by ui-developer → re-review APPROVE. Backend: read_credentials order_by(name, id) by developer. Docs synced by documenter (credential_sharing*.md, agent_api_tech.md, ssh/gsa/desktop tech docs, ui_ux_guidelines P9); docs check passes. Regression: running.
- 2026-10-16 — regression: tests/api/credentials/ 119/0, workspaces+agents/agent_api 129/0. DONE; uncommitted.
- 2026-10-16 — user: labels reverted to My/Automatic/Bundle Credentials (CredentialSectionTabs + docs); search box shrinks 160–256px to stay on the toolbar line at 1024.
- 2026-10-16 — user round 3: Notes dropped, header hidden (DataTable hideHeader, sr-only), Type/Sharing/Agents → CredentialRowBadges cluster with lazy Popover details (shares endpoint, deletion-impact affected_own_agents; shared-in rows count only). UI review PASS 10/10 r2; code review APPROVE after W1 keyboard Popover + S1 staleTime 30s; docs synced. Open nit: click on hover-opened card doesn't move focus. Dev DB lost shared-in rows mid-round (cause unknown).
