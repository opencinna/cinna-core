/**
 * Theme registry.
 *
 * Theming has two ORTHOGONAL axes, deliberately kept separate so they compose:
 *
 *   1. mode — "light" | "dark" | "system"   → `.dark` class on <html>
 *   2. skin — "default" | "hacker-1980" | …      → `data-skin="…"` on <html>
 *
 * Every skin must supply BOTH a light and a dark token set, so switching mode
 * never lands the user on an unstyled screen. The `.dark` class stays exactly
 * where it was, which means the ~600 `dark:` utilities scattered through the
 * app keep working untouched under every skin.
 *
 * A skin is a pure CSS-variable overlay — see `src/styles/skins/*.css`. It
 * redefines three tiers of tokens and NOTHING else:
 *
 *   Tier A — shadcn semantic tokens (--background, --card, --primary, …)
 *   Tier B — Tailwind palette scales (--color-blue-500, --color-amber-400, …)
 *            Tailwind v4 compiles `text-blue-500` to `var(--color-blue-500)`,
 *            so remapping the scale re-tints the ~1900 hardcoded status-colour
 *            utilities in the app without editing a single component.
 *   Tier C — decorative skin tokens (--skin-glow, --skin-grid, …) which the
 *            default skin defines as no-ops, so any component that opts into
 *            them stays safe in every skin.
 *
 * Adding a skin = one CSS file + one entry below. No component changes.
 */

export type SkinId = "default" | "hacker-1980"

export interface SkinDefinition {
  id: SkinId
  label: string
  description: string
  /** Swatch colours for the settings preview: [bg, surface, accent, text]. */
  preview: {
    light: [string, string, string, string]
    dark: [string, string, string, string]
  }
  /** Skins tuned primarily for one mode advertise it, so the UI can nudge. */
  prefersMode?: "light" | "dark"
  experimental?: boolean
}

export const SKINS: SkinDefinition[] = [
  {
    id: "default",
    label: "Cinna",
    description: "The standard product theme — soft surfaces, emerald accent.",
    preview: {
      light: ["#ffffff", "#f5f5f5", "#3d9b7c", "#171717"],
      dark: ["#242424", "#333333", "#4dbf99", "#fafafa"],
    },
  },
  {
    id: "hacker-1980",
    label: "Hacker 1980",
    description:
      "Retro vector-console look — near-black canvas, thin luminous cyan lines, monospaced uppercase labels and light-trace animations.",
    preview: {
      light: ["#eef6fa", "#ffffff", "#0091b8", "#04222c"],
      dark: ["#03080d", "#071219", "#4fd8ff", "#cdeefb"],
    },
    prefersMode: "dark",
    experimental: true,
  },
]

export const DEFAULT_SKIN: SkinId = "default"

export const getSkin = (id: string): SkinDefinition =>
  SKINS.find((s) => s.id === id) ?? SKINS[0]

export const isSkinId = (value: unknown): value is SkinId =>
  typeof value === "string" && SKINS.some((s) => s.id === value)
