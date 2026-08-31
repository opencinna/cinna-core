# Theming

## Purpose

Lets a user personalize the visual appearance of the whole app — color mode (light/dark/system) and a decorative "skin" layered on top — from a single **Theme and Colors** card in Settings. The choice is per-device, applies instantly across every page, and requires no reload.

## Core Concepts

Theming is built on two deliberately **orthogonal axes** that always compose:

- **Mode** — `light` / `dark` / `system`. Drives the `.dark` class on `<html>`. This is the pre-existing color-mode mechanism the app already had (via `frontend/src/components/theme-provider.tsx`); it is unchanged in this feature.
- **Skin** — `default` / `hacker-1980` (more can be registered). Drives a `data-skin="…"` attribute on `<html>`. This is new: a skin is a pure CSS custom-property overlay that restyles the app without touching any component.

Because the two axes are independent, every skin must ship **both** a light and a dark token set. Switching mode never lands the user on an unstyled screen, and the ~850 `dark:` Tailwind utilities already scattered through the app keep working untouched under any skin — a skin only changes what the CSS variables those utilities reference resolve to.

Registered skins today:
- **Cinna** (`default`) — the standard product theme: soft surfaces, emerald accent. No `data-skin` attribute is set for this skin — `:root` alone remains the token source when nothing is selected.
- **Hacker 1980** (`hacker-1980`) — retro vector-console look: near-black canvas, luminous cyan lines, monospaced uppercase labels, light-trace animations. Marked `experimental` and tuned primarily for dark mode (`prefersMode: "dark"`), but fully usable in light mode too.

## Settings UI

**Settings → Interface tab → "Theme and Colors" card** (`frontend/src/components/UserSettings/ThemeAndColors.tsx`), shown alongside Workspace, Agentic Team, and Dashboard settings. Available to every authenticated user regardless of role — there is no `agent-developer`/admin gate on this card or the Interface tab.

- **Color mode selector** — a three-button segmented control (Light / Dark / System, with Sun/Moon/Monitor icons) that sets the mode axis.
- **Skin picker** — one row per registered skin, each showing:
  - A **preview swatch**: a small four-stop mock panel (background, surface, accent, text) rendered from the skin's `preview.light` or `preview.dark` color arrays, whichever matches the *currently resolved* mode — so the swatch always reflects what the skin will actually look like right now, not a fixed marketing shot.
  - Label and description.
  - An **"Experimental" badge** when the skin declares `experimental: true`.
  - A **`prefersMode` nudge**: if the skin declares a preferred mode and the current resolved mode doesn't match, a small amber note reads *"Designed for {mode} mode — it works in both, but looks best there."* This is advisory only — nothing blocks selecting or using the skin in the non-preferred mode.
  - A checkmark on the currently-selected skin.

Selecting a mode or a skin applies immediately (no save button, no page reload).

## Persistence & Scope

Both choices are stored in the browser's `localStorage`, under two independent keys (mode: `vite-ui-theme`, skin: `vite-ui-skin`) — kept separate so mode and skin remain independently selectable rather than folded into one combined string. There is **no backend model, route, or database column** for theming; the choice is purely client-side, per-browser/device, and does not sync across devices or follow the account. See the [tech doc](theming_tech.md#backend-touchpoints) for how this was verified.

The app ships with `defaultTheme="dark"` (see `frontend/src/main.tsx`); skin defaults to `default` (Cinna) when nothing is stored.

## Integration Points

- **[Chat Windows](../chat_interface/chat_windows.md)** and every other surface built from `components/ui/*` shadcn primitives inherit skinning automatically — skins hang off the `data-slot` attributes those primitives already emit, so no chat, dashboard, or settings component needs to be aware theming exists.
- **Toasts** (`sonner`, mounted globally in `main.tsx`) now follow the app's own resolved mode instead of the OS-level preference — see the [tech doc](theming_tech.md#ui-primitive-changes) for the underlying bug this fixed.
- **Agent color presets** (`frontend/src/utils/colorPresets.ts`), used on the Dashboard header's agent/credential chips and the dashboard agent-selector pills, now carry a `data-color-preset` attribute so a skin can restyle color-coded chips without the preset table needing to know a skin exists.
- **Toggle switches** had a hardcoded accent color (`emerald-500`) replaced with the semantic `bg-primary` token, so they re-tint under any skin instead of staying a fixed green. Two places: the shared `components/ui/switch.tsx` primitive (used across Tasks, Agentic Teams, Admin, Agents, …) and the bespoke checkbox-based toggle in the **User Workspaces** settings card, which duplicates that primitive by hand. Other hand-rolled controls with baked-in accents may remain — a skin can reach tokens and primitives, but not hardcoded utility colors.

## Business Rules

- A skin is defined once, in `SKINS` (`frontend/src/config/themes.ts`), and applies everywhere with no per-page or per-component opt-in.
- The default skin is a no-op overlay: selecting `default` removes the `data-skin` attribute entirely rather than setting it to `"default"`.
- Any skin — including `experimental` ones — is freely selectable by any user today; `experimental` only changes the badge shown in the picker, it does not gate availability.
- `prefersMode` is a UI hint, never an enforcement: users can run any skin in either resolved mode.
