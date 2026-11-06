# Brand theme package template

A brand theme package is this folder: `theme.json` plus `assets/`. Copy it, then
replace the values with the brand's. The values here are stock Cinna, adjusted so
that every rule passes, and the two backgrounds are the stock mesh images.

## theme.json

| Key | What it is |
|---|---|
| `schema` | Always `cinna.brand-theme/1`. |
| `name` | The brand name, at most 40 characters. Used as the logo's alt text. |
| `modes.dark`, `modes.light` | One color for every role below, in each mode. Accepted forms: `#rgb`, `#rrggbb`, `rgb()`, `hsl()`, `oklch()`; no alpha, no named colors. |
| `logo` (optional) | `{"dark": "assets/logo-dark.svg", "light": "assets/logo-light.png"}`: both modes, SVG, PNG, JPEG or WebP. Left out: the Cinna wordmark, drawn with `logo.from` and `logo.to`. |
| `background` (optional) | `{"dark": "assets/bg-dark.webp", "light": null}`: both keys, each an image or `null` for a flat `surface.base`. Left out: flat in both modes. |
| `copy.newChatHeading` (optional) | The new-chat heading, plain text, at most 60 characters. |

Asset paths are relative to this folder and sit under `assets/`.

## Roles

Every role is required in both modes (35 roles, 70 values).

| Role | What it paints |
|---|---|
| `surface.base` | Window background. |
| `surface.raised` | Cards, sidebar and popovers. |
| `surface.tertiary` | Nested surfaces and muted fills. |
| `surface.hover` | Hovered rows and buttons. |
| `surface.input` | Text inputs and the chat composer. |
| `border` | Hairlines and outlines. |
| `text.primary` | Body text and headings. |
| `text.secondary` | Secondary text: labels, metadata. |
| `text.muted` | Least prominent text: hints, placeholders, timestamps. |
| `accent` | Primary action color: main buttons, links, focus rings. |
| `accent.hover` | The accent color while hovered. |
| `accent.on` | Text and icons drawn on top of the accent color. |
| `bubble.user` | Background of the user's own chat message bubble. |
| `highlight` | Agent highlight in dashboards. |
| `status.danger` | Fill and icon color for errors and destructive actions. |
| `status.warning` | Fill and icon color for warnings. |
| `status.success` | Fill and icon color for success. |
| `status.info` | Fill and icon color for neutral information. |
| `status.danger.text` | Error text on surface.base. |
| `status.warning.text` | Warning text on surface.base. |
| `status.success.text` | Success text on surface.base. |
| `status.info.text` | Information text on surface.base. |
| `chart.1` | Categorical chart slot 1, usually the accent. |
| `chart.2` | Categorical chart slot 2. |
| `chart.3` | Categorical chart slot 3. |
| `chart.4` | Categorical chart slot 4. |
| `chart.5` | Categorical chart slot 5. |
| `chart.6` | Categorical chart slot 6. |
| `chart.7` | Categorical chart slot 7. |
| `chart.8` | Categorical chart slot 8. |
| `chart.pos` | Positive end of a diverging scale. |
| `chart.neg` | Negative end of a diverging scale. |
| `chart.grid` | Chart grid hairlines. |
| `logo.from` | Start of the Cinna wordmark's stroke gradient (used when no logo is given). |
| `logo.to` | End of the Cinna wordmark's stroke gradient (used when no logo is given). |

## Assets

- `assets/bg-dark.webp`, `assets/bg-light.webp`: the stock Cinna mesh backgrounds
  (1600×1000 px is the minimum size for a raster background).
  Replace them with the brand's own, generated with `scripts/mesh`, or set
  `background` to `null` per mode for a flat window.
