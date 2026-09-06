import { Check, Copy, Download, ExternalLink, Monitor } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import useCustomToast from "@/hooks/useCustomToast"
import {
  cinnaConnectDeepLink,
  resolveApiBase,
  resolveServerOrigin,
} from "@/utils"

/**
 * Where to send someone we have no build for. The download resolver falls back
 * to this same index, so an arm64 Linux visitor lands in the same place either
 * way — just without first downloading an x86-64 binary that cannot run.
 */
const RELEASES_URL =
  "https://github.com/opencinna/cinna-desktop/releases/latest"

/**
 * The platforms the backend publishes an installer for. The resolver
 * (`GET /api/v1/desktop/download`) recognises exactly four combinations —
 * `darwin/arm64/dmg`, `darwin/x64/dmg`, `linux/x64/appimage`, `linux/x64/deb` —
 * and redirects anything else to the releases index. This section only ever
 * links to the four, so the fallback stays a safety net rather than a normal
 * path.
 */
type Platform = "darwin" | "linux"
type MacArch = "arm64" | "x64"
type LinuxKind = "appimage" | "deb"

/**
 * `navigator.userAgentData` is Chromium-only and absent from the DOM lib types,
 * so it is described here rather than widened globally.
 */
interface UserAgentDataLike {
  getHighEntropyValues?: (hints: string[]) => Promise<{
    architecture?: string
  }>
}

/**
 * Which OS to offer a build for, or `null` when we cannot tell.
 *
 * `null` is a real answer, not a failure: the section then offers both
 * platforms instead of guessing and rendering a button that installs the wrong
 * thing.
 */
function detectPlatform(): Platform | null {
  if (typeof navigator === "undefined") return null
  const ua = navigator.userAgent

  // Android's UA contains "Linux", and an iPad on iPadOS 13+ identifies as
  // "Macintosh". Both would otherwise be offered a desktop installer they
  // cannot run, so they are ruled out before the positive matches.
  if (/Android/i.test(ua)) return null
  if (/iPhone|iPod/i.test(ua)) return null
  if (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1) return null

  if (/Mac/i.test(ua)) return "darwin"
  // Not CrOS: ChromeOS also matches "X11 Linux" and has no build here.
  if (/CrOS/i.test(ua)) return null
  if (/Linux|X11/i.test(ua)) return "linux"
  return null
}

/**
 * The resolver URL for one platform/arch/format triple.
 *
 * Takes the API *base* (`resolveApiBase`), not the server origin: this is a
 * backend route, so it must carry whatever path prefix the deployment puts the
 * API behind, exactly as the generated client does.
 */
function downloadUrl(
  apiBase: string,
  os: Platform,
  arch: MacArch,
  kind: "dmg" | LinuxKind,
): string {
  const params = new URLSearchParams({ os, arch, kind })
  return `${apiBase}/api/v1/desktop/download?${params}`
}

/**
 * The desktop offer itself — download, deep link, server address — with no
 * page shell of its own.
 *
 * Three ways forward, in descending order of how much the visitor still has to
 * do: download the right build in one click, open an already-installed app via
 * the `cinna://` deep link, or copy the server address and type it in by hand.
 *
 * Deliberately a *section*, not a page: `DesktopLandingPage` wraps it in the
 * full-viewport card that `/desktop` has always rendered, and `/start` drops it
 * into one card among several. It therefore owns no heading and no centring —
 * an embeddable component that brings its own `min-h-screen` is not embeddable.
 */
export function DesktopDownloadSection() {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copied, setCopied] = useState(false)

  // Resolved once: all three are derived from build-time config and the page
  // URL, neither of which changes while the page is open.
  //
  // `apiBase` builds backend URLs (it may carry a path prefix); `serverOrigin`
  // is what the desktop app is handed. They differ behind a reverse proxy, and
  // `serverOrigin` is by construction the same value `cinnaConnectDeepLink`
  // embeds — so the address shown for copying is the one the deep link uses.
  const apiBase = useMemo(() => resolveApiBase(), [])
  const serverOrigin = useMemo(() => resolveServerOrigin(), [])
  const deepLink = useMemo(() => cinnaConnectDeepLink(), [])
  const [platform] = useState<Platform | null>(detectPlatform)

  // Apple Silicon and Intel are indistinguishable from the UA string alone.
  // `getHighEntropyValues` answers it on Chromium and is asynchronous, so the
  // page renders the arm64 default first and corrects itself if the answer
  // arrives. An explicit click always wins over a late detection result.
  const [detectedArch, setDetectedArch] = useState<MacArch | null>(null)
  const [chosenMacArch, setChosenMacArch] = useState<MacArch | null>(null)
  const [linuxKind, setLinuxKind] = useState<LinuxKind>("appimage")

  useEffect(() => {
    const uaData = (
      navigator as Navigator & { userAgentData?: UserAgentDataLike }
    ).userAgentData
    if (!uaData?.getHighEntropyValues) return

    let cancelled = false
    uaData
      .getHighEntropyValues(["architecture"])
      .then((values) => {
        if (cancelled || !values?.architecture) return
        setDetectedArch(values.architecture === "arm" ? "arm64" : "x64")
      })
      .catch(() => {
        // Permissions-Policy can reject the hint. The arm64 default and the
        // "Intel Mac?" link cover it; there is nothing to tell the user.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const macArch: MacArch = chosenMacArch ?? detectedArch ?? "arm64"
  const platforms: Platform[] =
    platform === null ? ["darwin", "linux"] : [platform]

  // The "copied" tick reverts on a timer, which must not outlive the component:
  // a `setCopied` after unmount is a React warning and, on a fast navigate-away,
  // a leaked timer per click.
  const copyResetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (copyResetTimer.current) clearTimeout(copyResetTimer.current)
    },
    [],
  )

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(serverOrigin)
      setCopied(true)
      showSuccessToast("Server address copied")
      if (copyResetTimer.current) clearTimeout(copyResetTimer.current)
      copyResetTimer.current = setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Failed to copy — select the address and copy it manually")
    }
  }

  return (
    <div className="space-y-4">
      {platform === null && (
        <p className="text-sm text-muted-foreground text-center">
          We couldn&rsquo;t tell which system you&rsquo;re on. Pick your
          platform:
        </p>
      )}

      {platforms.map((target, index) => (
        <div key={target} className="space-y-2">
          {target === "darwin" ? (
            <>
              <Button
                asChild
                className="w-full"
                variant={index === 0 ? "default" : "outline"}
              >
                <a href={downloadUrl(apiBase, "darwin", macArch, "dmg")}>
                  <Download className="mr-2 h-4 w-4" />
                  {/* Live region: the toggle below rewrites this label in
                      place, which is otherwise a silent change. */}
                  <span aria-live="polite">
                    Download for macOS (
                    {macArch === "arm64" ? "Apple Silicon" : "Intel"})
                  </span>
                </a>
              </Button>
              <button
                type="button"
                onClick={() =>
                  setChosenMacArch(macArch === "arm64" ? "x64" : "arm64")
                }
                className="mx-auto block text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
              >
                {macArch === "arm64"
                  ? "Intel Mac?"
                  : "Apple Silicon Mac (M1 or later)?"}
              </button>
            </>
          ) : detectedArch === "arm64" ? (
            /* Only x86-64 Linux assets are published, so an arm64 machine
               gets the releases index instead of a download button. Saying
               "x86-64 only" while still linking the x64 build was the worst
               of both: the truth on screen, the wrong binary one click
               away. The format toggle goes with it — there is nothing here
               to choose a format for. */
            <>
              <Button
                asChild
                className="w-full"
                variant={index === 0 ? "default" : "outline"}
              >
                <a
                  href={RELEASES_URL}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  <ExternalLink className="mr-2 h-4 w-4" />
                  Browse Linux releases
                </a>
              </Button>
              <p className="text-center text-xs text-muted-foreground">
                Linux builds are x86-64 only for now.
              </p>
            </>
          ) : (
            <>
              <Button
                asChild
                className="w-full"
                variant={index === 0 ? "default" : "outline"}
              >
                <a href={downloadUrl(apiBase, "linux", "x64", linuxKind)}>
                  <Download className="mr-2 h-4 w-4" />
                  {/* Live region: the toggle below rewrites this label in
                      place, which is otherwise a silent change. */}
                  <span aria-live="polite">
                    Download for Linux (
                    {linuxKind === "appimage" ? "AppImage" : ".deb"})
                  </span>
                </a>
              </Button>
              <button
                type="button"
                onClick={() =>
                  setLinuxKind(linuxKind === "appimage" ? "deb" : "appimage")
                }
                className="mx-auto block text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
              >
                {linuxKind === "appimage"
                  ? "Prefer a .deb package?"
                  : "Prefer the AppImage?"}
              </button>
            </>
          )}
        </div>
      ))}

      <Separator />

      <div className="space-y-2 text-center">
        <p className="text-sm text-muted-foreground">Already installed?</p>
        <Button asChild variant="outline" className="w-full">
          <a href={deepLink}>
            <Monitor className="mr-2 h-4 w-4" />
            Open Cinna Desktop
          </a>
        </Button>
      </div>

      <div className="space-y-2">
        <p className="text-xs text-muted-foreground text-center">
          &hellip;or enter this server address in the app:
        </p>
        {/* The *resolved* origin, never a hard-coded one: if this instance
            is misconfigured, a person reads the wrong address here instead
            of the desktop app failing silently against it. Same value the
            deep link above carries, so the two paths cannot diverge. */}
        <div className="flex items-center gap-2 rounded-lg border bg-muted/50 p-2">
          <code className="min-w-0 flex-1 select-all break-all font-mono text-xs">
            {serverOrigin}
          </code>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            onClick={handleCopy}
            aria-label="Copy the server address"
          >
            {copied ? (
              <Check className="h-3.5 w-3.5" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </Button>
        </div>
      </div>
    </div>
  )
}
