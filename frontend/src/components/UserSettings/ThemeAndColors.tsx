import { Check, Monitor, Moon, Sun } from "lucide-react"

import { type Theme, useTheme } from "@/components/theme-provider"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { SKINS, type SkinDefinition } from "@/config/themes"
import { cn } from "@/lib/utils"

const MODES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "system", label: "System", icon: Monitor },
]

/** Four-stop swatch (background, surface, accent, text) for the active mode. */
function SkinSwatch({
  skin,
  mode,
}: {
  skin: SkinDefinition
  mode: "light" | "dark"
}) {
  const [bg, surface, accent, text] = skin.preview[mode]

  return (
    <div
      className="flex h-12 w-20 shrink-0 flex-col justify-between overflow-hidden rounded-md border p-1.5"
      style={{ backgroundColor: bg, borderColor: `${accent}55` }}
      aria-hidden="true"
    >
      <div className="flex items-center gap-1">
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: accent }}
        />
        <span
          className="h-1 flex-1 rounded-full"
          style={{ backgroundColor: text, opacity: 0.55 }}
        />
      </div>
      <div
        className="h-4 w-full rounded-sm"
        style={{
          backgroundColor: surface,
          boxShadow: `0 0 6px -1px ${accent}`,
        }}
      />
    </div>
  )
}

export function ThemeAndColors() {
  const { theme, resolvedTheme, setTheme, skin, setSkin } = useTheme()

  return (
    <Card>
      <CardHeader>
        <CardTitle>Theme and Colors</CardTitle>
        <CardDescription>
          Choose the color mode and the visual theme of the interface. Saved on
          this device.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-2">
          <p className="text-sm font-medium">Color mode</p>
          <fieldset
            className="inline-flex rounded-md border p-1"
            aria-label="Color mode"
          >
            {MODES.map(({ value, label, icon: Icon }) => (
              <button
                key={value}
                type="button"
                onClick={() => setTheme(value)}
                aria-pressed={theme === value}
                data-testid={`theme-mode-${value}`}
                className={cn(
                  "flex items-center gap-2 rounded-sm px-3 py-1.5 text-sm transition-colors",
                  theme === value
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted",
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </button>
            ))}
          </fieldset>
        </div>

        <div className="space-y-2">
          <p className="text-sm font-medium">Theme</p>
          <div className="space-y-2">
            {SKINS.map((option) => {
              const selected = skin === option.id
              return (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => setSkin(option.id)}
                  aria-pressed={selected}
                  data-testid={`theme-skin-${option.id}`}
                  className={cn(
                    "flex w-full items-center gap-4 rounded-md border p-3 text-left transition-colors",
                    selected
                      ? "border-primary bg-primary/5"
                      : "hover:border-muted-foreground/50",
                  )}
                >
                  <SkinSwatch skin={option} mode={resolvedTheme} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">
                        {option.label}
                      </span>
                      {option.experimental && (
                        <Badge variant="secondary" className="text-[10px]">
                          Experimental
                        </Badge>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {option.description}
                    </p>
                    {option.prefersMode &&
                      option.prefersMode !== resolvedTheme && (
                        <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
                          Designed for {option.prefersMode} mode — it works in
                          both, but looks best there.
                        </p>
                      )}
                  </div>
                  {selected && (
                    <Check className="h-4 w-4 shrink-0 text-primary" />
                  )}
                </button>
              )
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
