# Theming — Technical Reference

## File Locations

### Frontend — Registry & State

- `frontend/src/config/themes.ts` — `SkinId`, `SkinDefinition`, the `SKINS` registry, `DEFAULT_SKIN`, `getSkin()`, `isSkinId()`. The file's top-of-file docblock is the canonical architecture explanation for this feature — start there.
- `frontend/src/components/theme-provider.tsx` — `ThemeProvider` / `useTheme()`: owns both the mode (`theme`, `resolvedTheme`) and skin (`skin`) state, applies both to `<html>`, and persists both to `localStorage`.
- `frontend/src/components/UserSettings/ThemeAndColors.tsx` — the Settings card: mode segmented control, skin picker list, `SkinSwatch` preview renderer.
- `frontend/src/routes/_layout/settings.tsx` — mounts `ThemeAndColors` in the "Interface" tab, alongside `WorkspaceSettings`, `AgenticTeamSettings`, `DashboardSettings`.
- `frontend/src/main.tsx` — mounts `ThemeProvider` at the app root: `<ThemeProvider defaultTheme="dark" storageKey="vite-ui-theme">` (skin uses its defaults: `defaultSkin=DEFAULT_SKIN`, `skinStorageKey="vite-ui-skin"`).

### Frontend — Skin CSS

- `frontend/src/index.css` — imports every skin CSS file (`@import "./styles/skins/hacker-1980.css";`) and defines the **Tier C default no-op tokens** (`--skin-line`, `--skin-glow`, `--skin-glow-soft`, `--skin-grid`, `--skin-grid-size`, `--skin-scanline`, `--skin-surface-tint`, `--skin-label-color`, `--skin-sweep`) on `:root`, alongside the existing Tier A tokens (`--background`, `--card`, `--primary`, …).
- `frontend/src/styles/skins/hacker-1980.css` — the one shipped non-default skin: a self-contained overlay, described in detail below.

### Frontend — Components Touched to Support Skinning

- `frontend/src/components/ui/button.tsx` — adds `data-ui="button"` (applied *after* the props spread).
- `frontend/src/components/ui/dropdown-menu.tsx` — `DropdownMenuSubContent` now renders inside a `DropdownMenuPrimitive.Portal`.
- `frontend/src/components/ui/sonner.tsx` — reads mode from the app's own `useTheme()` instead of `next-themes`.
- `frontend/src/components/ui/switch.tsx` — `data-[state=checked]:bg-emerald-500` → `data-[state=checked]:bg-primary`.
- `frontend/src/components/UserSettings/WorkspaceSettings.tsx` — its own hand-rolled toggle switch: `bg-emerald-500` → `bg-primary` (same fix as `ui/switch.tsx`, but this component doesn't use the shared primitive).
- `frontend/src/components/Dashboard/DashboardHeader.tsx` and `frontend/src/routes/_layout/index.tsx` — add `data-color-preset={colorPreset.value}` to agent/credential chips rendered with a color preset (see `frontend/src/utils/colorPresets.ts`), and `data-ui="new-agent"` to the dashboard's "New Agent" button.

### Backend Touchpoints

None. Verified by inspecting `backend/app/models/`, `backend/app/api/routes/`, and `backend/app/services/` for any `skin`/`theme` reference — there is no model, route, service, or migration for theming. The feature is entirely client-side state persisted in `localStorage`.

## The Two Axes, Mechanically

`theme-provider.tsx` owns both:

- **Mode** (`theme: "light" | "dark" | "system"`, pre-existing): on change, `document.documentElement.classList` is toggled between `"light"`/`"dark"` (resolving `"system"` via `window.matchMedia("(prefers-color-scheme: dark)")`, with a `change` listener kept live while `theme === "system"`). `resolvedTheme` is the always-concrete `"light" | "dark"` derived value, exposed to consumers that need to know the *actual* mode (e.g. skin swatches, Sonner).
- **Skin** (`skin: SkinId`, new): on change, `document.documentElement` gets `setAttribute("data-skin", skin)`, or `removeAttribute("data-skin")` when `skin === DEFAULT_SKIN`.

Both are independent `useState` + `useEffect` pairs in the same provider; there is no coupling between the two effects beyond both targeting `<html>`.

## Persistence

| Axis | `localStorage` key | Default | Setter |
|------|--------------------|---------|--------|
| Mode | `vite-ui-theme` (configurable via `storageKey` prop) | `"dark"` (set in `main.tsx`; provider's own fallback is `"system"`) | `setTheme(theme)` — writes to storage then calls `setState` |
| Skin | `vite-ui-skin` (configurable via `skinStorageKey` prop) | `DEFAULT_SKIN` (`"default"`) | `setSkin(skin)` — writes to storage then calls `setState` |

Both keys are read once on mount via `useState(() => …)` lazy initializers; the skin initializer runs the stored value through `isSkinId()` so a corrupted or stale value falls back to `defaultSkin` instead of producing an invalid `data-skin`.

## The `SKINS` Registry

```ts
export type SkinId = "default" | "hacker-1980"

export interface SkinDefinition {
  id: SkinId
  label: string
  description: string
  preview: {
    light: [string, string, string, string]  // [bg, surface, accent, text]
    dark: [string, string, string, string]
  }
  prefersMode?: "light" | "dark"
  experimental?: boolean
}

export const SKINS: SkinDefinition[] = [ /* "default", "hacker-1980" */ ]
export const DEFAULT_SKIN: SkinId = "default"
export const getSkin = (id: string): SkinDefinition =>
  SKINS.find((s) => s.id === id) ?? SKINS[0]
export const isSkinId = (value: unknown): value is SkinId =>
  typeof value === "string" && SKINS.some((s) => s.id === value)
```

`getSkin()` always returns a valid definition (falls back to `SKINS[0]`, i.e. `default`) — safe to call with an unvalidated id. `isSkinId()` is the type guard used by `theme-provider.tsx` to sanitize whatever comes out of `localStorage`.

## Token Tiers

A skin CSS file may define exactly three tiers of custom properties, plus presentational rules scoped under its own `[data-skin="…"]` selector. Nothing else — no skin file should touch component markup or add new selectors unscoped to its own attribute.

### Tier A — shadcn semantic tokens

The existing design-system tokens: `--background`, `--foreground`, `--card`, `--card-foreground`, `--popover`, `--primary`, `--secondary`, `--muted`, `--accent`, `--destructive`, `--border`, `--input`, `--ring`, `--chart-1..5`, `--sidebar*`, `--radius`. These are what every shadcn `ui/` primitive already consumes. A skin redefines them once for `.dark` and once for the non-dark (light) case.

### Tier B — Tailwind v4 palette scales

`--color-blue-500`, `--color-amber-400`, etc. — the actual Tailwind color-scale variables. Tailwind v4 compiles a utility class like `text-blue-500` directly to `color: var(--color-blue-500)`. This means **redefining the scale re-tints every hardcoded status-color utility already in the app** (the codebase has roughly 1,900 such call sites — `text-red-600`, `bg-emerald-500/10`, etc.) without editing a single component. `hacker-1980.css` remaps all twelve Tailwind color families (blue/sky/cyan/indigo/violet/purple → cold cyan family; green/emerald/teal/lime → aqua-green; amber/yellow/orange → amber; red/rose/pink/fuchsia → hot orange-red; slate/gray/zinc/neutral/stone → cold near-achromatic neutrals) to `oklch(...)` values that preserve Tailwind's original lightness ladder step-for-step — only hue and chroma move, so light-on-dark contrast decisions made elsewhere in the app remain valid.

### Tier C — decorative skin tokens

`--skin-line`, `--skin-glow`, `--skin-glow-soft`, `--skin-grid`, `--skin-grid-size`, `--skin-scanline`, `--skin-surface-tint`, `--skin-label-color`, `--skin-sweep`. These don't correspond to any existing design-system concept — they're skin-specific decoration hooks (glow color, grid line color/size, scanline overlay, card surface tint, label color, CTA sheen color). The **default skin defines every one of these as a no-op** in `index.css` (transparent / `var(--card)` / `inherit` / `0`), so any component or skin effect that references a Tier C token is always safe under the default skin — it just renders nothing extra. A new skin gives them actual values to switch on real decoration.

## `hacker-1980.css` — Anatomy of a Skin

`frontend/src/styles/skins/hacker-1980.css`, imported once from `index.css`, entirely scoped under `[data-skin="hacker-1980"]` (or its `.dark` / `:not(.dark)` compounds):

1. **Tier B block** (`[data-skin="hacker-1980"] { … }`) — the full palette remap described above, shared by both modes.
2. **Tier A+C dark block** (`:root[data-skin="hacker-1980"].dark { … }`) — the canonical look: near-black `--background`, luminous cyan `--primary`/`--skin-line`, translucent borders/rings tied to the accent hue.
3. **Tier A+C light block** (`:root[data-skin="hacker-1980"]:not(.dark) { … }`) — the same geometry inverted, kept in lockstep with the dark block field-for-field so mode switching never desyncs.
4. **Effects** — presentational rules that hang entirely off shadcn's `data-slot` attributes (`[data-slot="card"]`, `[data-slot="dialog-content"]`, `[data-slot="table-row"]`, …) plus the two attribute hooks added for this feature (`data-ui="button"`, `data-color-preset`): scanline/grid canvas background, monospace uppercase card/dialog/table headers, corner-bracket card frames with a clip-path reveal animation, glowing focus rings, an underlit active-tab trace, themed Sonner toasts (driven through Sonner's own `--normal-*`/`--success-*`/etc. CSS variables since Sonner injects its stylesheet at runtime and source order isn't guaranteed), per-color-preset hairline outlines derived from `currentColor`, and a one-shot sheen animation on the "New Agent" button. A `@media (prefers-reduced-motion: reduce)` block disables the two animations.

### Specificity Note

Base Tier A tokens live on `:root` / `.dark` (specificity 0,1,0). The skin's mode blocks are `:root[data-skin="…"].dark` / `:root[data-skin="…"]:not(.dark)` (0,2,0+), so neither the base theme nor the opposite mode block can out-specify the active one regardless of CSS source order.

## `theme-provider.tsx` Apply/Persist Flow

```
mount → read localStorage[storageKey] / localStorage[skinStorageKey]
      → lazy useState initializers (skin sanitized via isSkinId)

theme changes  → updateTheme(): classList.remove("light","dark"); classList.add(resolved)
              → resolvedTheme state updated
              → (system) matchMedia("(prefers-color-scheme: dark)") change listener re-resolves live

skin changes   → skin === DEFAULT_SKIN ? removeAttribute("data-skin") : setAttribute("data-skin", skin)

setTheme(t) / setSkin(s) exposed via context → each writes localStorage THEN calls the state setter
```

Both effects run independently (`useEffect(…, [skin])` vs the mode effect keyed on `[theme, updateTheme, getResolvedTheme]`) — there is no shared effect or ordering dependency between the two axes.

## UI Primitive Changes

These four `components/ui/*` edits exist purely to make skinning correct/possible; none change default-skin appearance:

- **`button.tsx` → `data-ui="button"`**: `data-slot="button"` gets silently overwritten whenever a `Button` is the child of an `asChild` trigger (`DialogTrigger`, `DropdownMenuTrigger`, …) — the trigger's own `data-slot` lands via the `{...props}` spread, after JSX's own `data-slot="button"` attribute. `data-ui="button"` is placed in JSX *after* `{...props}`, so it always survives regardless of nesting, giving skins a reliable "this is a button" selector hook (`hacker-1980.css` uses `[data-ui="button"]` for its monospace/uppercase button styling).
- **`dropdown-menu.tsx` → `SubContent` now portalled**: without the portal, `SubContent` rendered as a DOM descendant of `DropdownMenuContent`, which is a scroll container (`overflow-x-hidden overflow-y-auto`) — so a submenu positioned beside its parent (not inside it) was visually clipped to a sliver. This is the pattern Radix documents for Sub menus; unrelated to any specific skin, but was surfaced while building the hacker-1980 overlay's menu styling.
- **`sonner.tsx` → app `useTheme()` instead of `next-themes`**: there is no `NextThemesProvider` anywhere in the tree, so `useTheme()` from the `next-themes` package always fell back to its own default (`"system"`) — toasts silently followed the OS color scheme rather than the in-app mode selection, pre-existing bug now fixed as part of wiring theming end-to-end. Sonner is handed `resolvedTheme` (already concrete `light`/`dark`) directly instead of the raw `theme` value, avoiding Sonner re-resolving `"system"` a second time.
- **`switch.tsx`** and **`WorkspaceSettings.tsx`**'s hand-rolled toggle: both had `bg-emerald-500` hardcoded for the checked state — a Tier A/B-invisible color that would not re-tint under any skin. Both now use `bg-primary`, which is a Tier A token every skin (including `hacker-1980`) already redefines.

## `data-color-preset` and `data-ui="new-agent"`

`frontend/src/utils/colorPresets.ts` defines twelve named `ColorPreset` values (`slate`, `blue`, `indigo`, `purple`, `pink`, `rose`, `orange`, `amber`, `emerald`, `teal`, `cyan`, `sky`) used for agent identity badges. `DashboardHeader.tsx` (sidebar agent/credential chips) and `routes/_layout/index.tsx` (dashboard agent-selector pills) now stamp `data-color-preset={colorPreset.value}` on each chip. `hacker-1980.css` uses `[data-color-preset]` to draw a `currentColor`-derived hairline outline on every chip in one rule (covering all twelve presets and any added later), with a dedicated `[data-color-preset="slate"]` override since `slate` is the "no color assigned" fallback and, unlike the other eleven, uses an opaque `-800` dark fill rather than a translucent `-900/40` one.

The hairline is drawn with `outline` + `outline-offset: -1px`, **not** `box-shadow`. This is load-bearing: the selected chip carries a Tailwind `ring-2`, and in Tailwind v4 `ring-*` is itself implemented as a `box-shadow` — so a skin that paints its own `box-shadow` on the same element silently erases the selection indicator. The same caution applies to any future skin rule targeting an element that may carry `ring-*`.

`data-ui="new-agent"` on the dashboard's "New Agent" button lets `hacker-1980.css` give it a distinct treatment (armed-console-key look with a one-shot sweep animation) rather than folding it into the generic color-preset styling, mirroring how the default skin already treats it specially (blue→purple gradient) among otherwise-flat chips.

## Adding a New Skin

1. Create a new CSS file in `frontend/src/styles/skins/` named after the skin id (e.g. `my-skin.css`, following `hacker-1980.css`). Scope every rule under `[data-skin="my-skin"]` (or the `.dark` / `:not(.dark)` compounds for mode-specific token blocks). Define:
   - Tier A overrides for `.dark` and for `:not(.dark)` — every field the default skin sets in `index.css`'s `:root` block, for both modes.
   - Optional Tier B palette remap if the skin wants to re-tint hardcoded status colors.
   - Optional Tier C values (`--skin-*`) if the skin wants bespoke decoration; anything left unset inherits the default no-op from `index.css`.
   - Optional presentational "Effects" rules hung off `data-slot="…"` / `data-ui="…"` / `data-color-preset` attributes.
2. Add an `@import "./styles/skins/my-skin.css";` line in `frontend/src/index.css`, alongside the existing skin import.
3. Add one entry to `SKINS` in `frontend/src/config/themes.ts`: `id`, `label`, `description`, `preview.light`/`preview.dark` four-stop swatch colors (background/surface/accent/text — used by `SkinSwatch` in the Settings picker), and optionally `prefersMode` / `experimental`.
4. No component changes are required — the Settings picker (`ThemeAndColors.tsx`) renders every entry in `SKINS` automatically, and every `ui/` primitive already emits the `data-slot`/`data-ui` hooks a new skin's effects can target.
5. Verify both light and dark render correctly for the new skin before committing — nothing enforces that the two mode blocks stay in sync field-for-field; a missing field silently falls through to whatever `:root`/`.dark` left behind, which uses the Cinna default's raw value, not the new skin's.

## Key Libraries / Mechanisms

- Tailwind v4 `@theme inline` + CSS custom properties — the whole tiering scheme depends on Tailwind v4 compiling utility classes to `var(--color-*)` lookups rather than baking static color values at build time.
- `oklch()` color space — used throughout `hacker-1980.css` so hue/chroma can move independently of the lightness ladder inherited from Tailwind's default palette.
- `color-mix(in oklab, …)` — used for hover/derived tints (e.g. `color-mix(in oklab, var(--skin-line) 55%, transparent)`) instead of separate hardcoded hover colors.
- `[data-slot]` — the shadcn/ui convention (already present on every primitive in `components/ui/`) that skin "Effects" rules select against; no primitive needed modification to become skinnable, only `button.tsx` and `dropdown-menu.tsx` needed the two fixes described above.
