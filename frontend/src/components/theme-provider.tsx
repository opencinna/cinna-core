import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react"

import { DEFAULT_SKIN, isSkinId, type SkinId } from "@/config/themes"

export type Theme = "dark" | "light" | "system"

type ThemeProviderProps = {
  children: React.ReactNode
  defaultTheme?: Theme
  defaultSkin?: SkinId
  storageKey?: string
  skinStorageKey?: string
}

type ThemeProviderState = {
  /** Colour mode preference, including the literal "system". */
  theme: Theme
  /** Colour mode actually in effect right now. */
  resolvedTheme: "dark" | "light"
  setTheme: (theme: Theme) => void
  /** Visual skin layered on top of the mode. */
  skin: SkinId
  setSkin: (skin: SkinId) => void
}

const initialState: ThemeProviderState = {
  theme: "system",
  resolvedTheme: "light",
  setTheme: () => null,
  skin: DEFAULT_SKIN,
  setSkin: () => null,
}

const ThemeProviderContext = createContext<ThemeProviderState>(initialState)

export function ThemeProvider({
  children,
  defaultTheme = "system",
  defaultSkin = DEFAULT_SKIN,
  storageKey = "vite-ui-theme",
  skinStorageKey = "vite-ui-skin",
  ...props
}: ThemeProviderProps) {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem(storageKey) as Theme) || defaultTheme,
  )

  // Skin lives in its own storage key rather than being folded into the theme
  // string, precisely so mode and skin stay independently selectable.
  const [skin, setSkin] = useState<SkinId>(() => {
    const stored = localStorage.getItem(skinStorageKey)
    return isSkinId(stored) ? stored : defaultSkin
  })

  const getResolvedTheme = useCallback((theme: Theme): "dark" | "light" => {
    if (theme === "system") {
      return window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
    }
    return theme
  }, [])

  const [resolvedTheme, setResolvedTheme] = useState<"dark" | "light">(() =>
    getResolvedTheme(theme),
  )

  const updateTheme = useCallback((newTheme: Theme) => {
    const root = window.document.documentElement

    root.classList.remove("light", "dark")

    if (newTheme === "system") {
      const systemTheme = window.matchMedia("(prefers-color-scheme: dark)")
        .matches
        ? "dark"
        : "light"

      root.classList.add(systemTheme)
      return
    }

    root.classList.add(newTheme)
  }, [])

  useEffect(() => {
    updateTheme(theme)
    setResolvedTheme(getResolvedTheme(theme))

    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)")

    const handleChange = () => {
      if (theme === "system") {
        updateTheme("system")
        setResolvedTheme(getResolvedTheme("system"))
      }
    }

    mediaQuery.addEventListener("change", handleChange)

    return () => {
      mediaQuery.removeEventListener("change", handleChange)
    }
  }, [theme, updateTheme, getResolvedTheme])

  useEffect(() => {
    const root = window.document.documentElement
    // The default skin carries no attribute at all, so `:root` alone remains
    // the authoritative token source when no skin is selected.
    if (skin === DEFAULT_SKIN) {
      root.removeAttribute("data-skin")
    } else {
      root.setAttribute("data-skin", skin)
    }
  }, [skin])

  const value: ThemeProviderState = {
    theme,
    resolvedTheme,
    setTheme: (theme: Theme) => {
      localStorage.setItem(storageKey, theme)
      setTheme(theme)
    },
    skin,
    setSkin: (skin: SkinId) => {
      localStorage.setItem(skinStorageKey, skin)
      setSkin(skin)
    },
  }

  return (
    <ThemeProviderContext.Provider {...props} value={value}>
      {children}
    </ThemeProviderContext.Provider>
  )
}

export const useTheme = () => {
  const context = useContext(ThemeProviderContext)

  if (context === undefined)
    throw new Error("useTheme must be used within a ThemeProvider")

  return context
}
