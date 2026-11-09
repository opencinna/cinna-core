---
name: brand-theme
description: Turn a company brandbook into a Cinna brand theme package (theme.json with colors for every role in dark and light mode, an optional logo and backgrounds) that a Cinna server admin uploads to brand Cinna Desktop and the sign-in pages. Includes the server's own validator, a gradient-mesh background generator, a visual preview and the zip packager.
when_to_use: Someone asks to brand Cinna, make a Cinna theme from a brandbook, logo or brand colors, adjust or fix a brand theme package, generate theme backgrounds, or fix errors a Cinna server reported when a theme was uploaded.
version: "1"
---

# Brand theme

A brand theme is a folder: `theme.json` plus `assets/`. The server checks it
on upload and refuses anything that breaks a rule, with errors naming the exact
value to change. This skill ships the same validator, so you can get a package
clean before anyone uploads it.

All scripts live in this skill's `scripts/` folder and are run with
`uv run --script <this skill's folder>/scripts/<name> …` (each declares its own
dependencies; the first run downloads them). They have no `.py` extension on
purpose. Run them from the folder that holds your package.

## Workflow

1. **Start from the template.** Copy `template/` (beside this file) to a new
   folder named after the brand, for example `acme-theme/`. It holds stock
   Cinna colors that already pass every rule, and two stock backgrounds.
   `template/README.md` lists every role with what it paints; read it before
   choosing colors.

2. **Pick colors from the brandbook, for both modes.** Set `name` (at most 40
   characters) and every role under `modes.dark` and `modes.light`; each mode
   needs all roles. Map the brand's primary color to `accent` (and usually
   `chart.1`), its neutrals to `surface.*`, `border` and `text.*`, and its
   secondary palette to `chart.2`… `chart.8`. Write literal colors only: `#rgb`,
   `#rrggbb`, `rgb()`, `hsl()` or `oklch()`; no alpha, no names, no `var()`.
   Dark mode is not the light palette inverted: keep surfaces dark, lift text
   and accents until they read. A brand red usually belongs in
   `status.danger`; if the brand's accent is red, the validator refuses an
   accent that reads as danger, so move `status.danger` to a different hue.
   Optionally set `copy.newChatHeading` (one line, at most 60 characters).

3. **Export the logo (optional).** One file per mode under `assets/`, SVG
   (preferred) or PNG, JPEG or WebP, then
   `"logo": {"dark": "assets/logo-dark.svg", "light": "assets/logo-light.svg"}`.
   The dark-mode logo must read on a dark surface. Rasters need at least 124 px
   of height; the logo is drawn inside a 240×62 box, so anything wider than
   10:1 is refused. SVGs are sanitised: no scripts, images, fonts, `<text>`
   (convert text to outlines), `<style>` or external references. Without a
   logo, the Cinna wordmark is drawn with `logo.from` → `logo.to`.

4. **Backgrounds (optional).** Either take them from the brandbook (at least
   1600×1000 px, PNG, JPEG or WebP up to 3 MB, or SVG), or generate meshes
   from the theme's own colors:

   ```
   uv run --script <skill>/scripts/mesh acme-theme --mode both --candidates 4
   ```

   This writes `assets/bg-dark-s7.webp`, `assets/bg-light-s7.webp`, … one pair
   per seed. Options: `--colors accent,chart.2,#7a3cff` (roles or literal
   colors, such as a secondary brand color with no role), `--seed N`,
   `--softness 0.0–1.0`, `--strength auto|0.0–1.0`, `--format webp|svg`,
   `--out DIR`. `auto` makes the blobs as strong as the readability rule
   allows, with a margin, and prints the strength it chose. A seed always gives
   the same image, so "seed 9, softer" is `--seed 9 --softness 0.8`.
   If mesh says even a faint mesh fails, the text colors are the problem, not
   the image: fix `text.primary` / `text.secondary` against `surface.base`.

   Readability: `text.primary` and `text.secondary` must reach 4.5:1 on 95 % of
   the image. A soft, low-contrast image (a mesh, a heavy blur, a subtle
   texture) passes; a photo or a busy pattern usually does not. Set a mode to
   `null` for a flat `surface.base` window. Point `background` at the chosen
   pair: `"background": {"dark": "assets/bg-dark-s7.webp", "light": "assets/bg-light-s7.webp"}`.

5. **Validate, fix, repeat.**

   ```
   uv run --script <skill>/scripts/validate acme-theme
   ```

   Each line is `ERROR <path> [<code>]: <what to change>`, for example
   `ERROR modes.dark.text.muted [low_contrast]: …`. Fix the named value and run
   it again until it prints `VALID`. Exit code 0 means valid, 1 invalid, 2 the
   package could not be read. `WARN` lines never block (an unused background
   candidate is a warning). `--json` prints the same as JSON. These are the
   server's own rules: a package that passes here passes on upload.

6. **Preview and hand over.**

   ```
   uv run --script <skill>/scripts/preview acme-theme
   uv run --script <skill>/scripts/package acme-theme
   ```

   `preview` writes `acme-theme/preview.html`: a mock of the desktop main
   screen and the sign-in card in both modes, one row per background candidate
   with whether the validator accepts it. Look at it yourself (contrast is
   measured, taste is not), then give it to the admin to choose from.
   `package` validates again and writes `acme-theme.zip` with `theme.json` and
   only the assets `theme.json` uses. Hand the admin the zip and the preview.

## Handing it to the admin

A server superuser uploads the zip as a draft, previews it and publishes it
(Admin → Server Configuration → Interface → Brand theme, or `POST /api/v1/admin/brand-theme/drafts` with
the zip as `package`). Every Cinna Desktop on that server then takes the theme,
and so do the server's sign-in pages. A change is a new upload, not an edit.

## Rules worth knowing up front

- Text roles need 4.5:1 against the surfaces they sit on; `text.muted`,
  `accent`, `status.*` and the chart colors need 3:1 against `surface.base`;
  `accent.on` needs 4.5:1 on `accent` and `accent.hover`.
- The eight chart slots must be clearly distinct from each other, and
  `bubble.user` must differ visibly from `surface.base`.
- Unknown keys are refused, so a typo in a role name is an error, not a no-op.
- The dark background must be darker than the light one.
