---
feature: server_configuration
domain: application
one_liner: "Admin-configurable server-wide settings for the login disclaimer, registration and sign-in policy, the public landing page, channels, and mail servers."
docs:
  disclaimer: disclaimer.md
  disclaimer tech: disclaimer_tech.md
  access policy: access_policy.md
  access policy tech: access_policy_tech.md
  landing page: landing_page.md
  landing page tech: landing_page_tech.md
---
# Server Configuration

## Purpose

`/admin/server-configuration` is the superuser's single place to configure server-wide, instance-level settings — as opposed to per-agent or per-user settings, which live on their own pages. Everything on this page is backed by one singleton `ServerConfig` row (plus, for Channels and Mail Servers, their own dedicated tables), so there is exactly one configuration per deployment, not one per user.

The page is split into four tabs:

- **Interface** — the [Disclaimer](disclaimer.md) card (a Markdown notice shown at login in a non-dismissible blocking modal) and the [Local Agent Kit](../local_agent_kit/local_agent_kit.md) publish toggle. Both cards live here because they are instance-wide, cosmetic-or-onboarding switches rather than access control.
- **Access** — a two-column grid of front-door-policy cards (`RegistrationCard`, `SignInMethodsCard`, `NewUserDefaultsCard`, `LandingPageCard`) documented in [Access Policy](access_policy.md) and [Public Landing Page](landing_page.md), plus `CompanyAiCredentialsCard`, which edits `AIProvider.auto_provision_roles` rather than any `ServerConfig` column (see [admin_ai_credential_provisioning](../ai_credentials/admin_ai_credential_provisioning.md)). All cards on this tab are half-width with icon titles.
- **Channels** — `ServerChannelsCard`, `AutoInstallAgentsCard`, and `ServerDebugToolsCard`. This tab is the admin surface for the [Server Channels](../server_channels/server_channels.md) feature; channel CRUD, the auto-install bundle list, and the live per-channel debug feed all live here, not on this feature's own docs.
- **Mail Servers** — `MailServersCard`. A peer of the Channels tab rather than a part of it: an email channel references a mail server by id, and mail servers outlive any one channel. Documented under [mail_servers](../email_integration/mail_servers.md).

## The three documented sub-features

- **[Disclaimer](disclaimer.md)** — superuser enables/disables a Markdown notice shown at login. Display mode controls whether it reappears once per browser (`new_users`) or once per session (`every_login`); any content or mode edit bumps `disclaimer_version`, which invalidates every previously stored browser acknowledgement so all users see the update.
- **[Access Policy](access_policy.md)** — six `ServerConfig` columns deciding who may get an account and how they sign in: `registration_mode` (`open` / `invite_only`), `allowed_email_patterns` (comma-separated globs; empty and `*` both mean no restriction; gates registration only, never login), `password_auth_enabled`, `google_auto_register`, `default_user_role`, and `invite_include_desktop_default`. Replaces the old `AUTH_WHITELIST_USER_DOMAINS` and `DEFAULT_USER_ROLE` env settings, which now act as first-boot seeds only. A public, rate-limited `GET /server-config/access-policy` projection (no patterns, no role) drives `/login` and `/signup`.
- **[Public Landing Page](landing_page.md)** — `ServerConfig.landing_markdown`, admin-authored Markdown rendered on the public, unguarded `/start` route: one stable address to paste into a wiki, showing whichever sign-in methods the instance offers plus the desktop download. Served by its own public `GET /server-config/landing` projection, separate from the access-policy projection since it is content rather than policy. `None` means unchanged, `""` clears it; never bumps `disclaimer_version`; 16 KiB cap; a write-path `reject_raw_html` validator. The render path (`components/Landing/LandingMarkdown.tsx`) has its own plugin list with no `rehype-raw` and runs `rehype-sanitize`, so a change to the chat renderer cannot reach `/start`.

## Integration Points

- **Server Channels** — the Channels tab is this feature's admin surface for [server_channels](../server_channels/server_channels.md); channel CRUD, auto-install list, and debug tooling are documented there, not here.
- **Mail Servers** — the Mail Servers tab is this feature's admin surface for [mail_servers](../email_integration/mail_servers.md).
- **Local Agent Kit** — the Interface tab's publish toggle belongs to [local_agent_kit](../local_agent_kit/local_agent_kit.md); it shares this feature's `["serverConfig"]` query key and update endpoint.
- **Admin AI Credential Provisioning** — the Access tab's Company AI providers card edits `AIProvider.auto_provision_roles`; see [admin_ai_credential_provisioning](../ai_credentials/admin_ai_credential_provisioning.md).
- **Invitations** — `AccountOrigin.INVITE` is exempt from the access policy's `allowed_email_patterns`, so an administrator may invite an address outside the pattern list, and in invite-only mode; the pattern list bounds self-service registration, not administrator intent. See [Access Policy](access_policy.md#core-concepts).
