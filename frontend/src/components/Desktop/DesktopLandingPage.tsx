import { Check, Copy, Download, Monitor } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import useCustomToast from "@/hooks/useCustomToast"
import { cinnaConnectDeepLink, resolveApiOrigin } from "@/utils"

/**
 * The platforms the backend publishes an installer for. The resolver
 * (`GET /api/v1/desktop/download`) recognises exactly four combinations —
 * `darwin/arm64/dmg`, `darwin/x64/dmg`, `linux/x64/appimage`, `linux/x64/deb` —
 * and redirects anything else to the releases index. This page only ever links
 * to the four, so the fallback stays a safety net rather than a normal path.
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
 * `null` is a real answer, not a failure: the page then offers both platforms
 * instead of guessing and rendering a button that installs the wrong thing.
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

/** The resolver URL for one platform/arch/format triple, on the API origin. */
function downloadUrl(
  apiOrigin: string,
  os: Platform,
  arch: MacArch,
  kind: "dmg" | LinuxKind,
): string {
  const params = new URLSearchParams({ os, arch, kind })
  return `${apiOrigin}/api/v1/desktop/download?${params}`
}

/**
 * Public onboarding page for Cinna Desktop, linked from the new-account email.
 *
 * Three ways forward, in descending order of how much the visitor still has to
 * do: download the right build in one click, open an already-installed app via
 * the `cinna://` deep link, or copy the server address and type it in by hand.
 */
export function DesktopLandingPage() {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copied, setCopied] = useState(false)

  // Resolved once: both values are derived from build-time config and the page
  // URL, neither of which changes while the page is open.
  const apiOrigin = useMemo(() => resolveApiOrigin(), [])
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

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(apiOrigin)
      setCopied(true)
      showSuccessToast("Server address copied")
      setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Failed to copy — select the address and copy it manually")
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4 bg-background">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <Monitor className="mx-auto h-12 w-12 text-primary" />
          <CardTitle className="mt-2">Get Cinna Desktop</CardTitle>
          <CardDescription>
            Install the desktop app and sign in once — it sets itself up from
            there.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
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
                    <a href={downloadUrl(apiOrigin, "darwin", macArch, "dmg")}>
                      <Download className="mr-2 h-4 w-4" />
                      Download for macOS (
                      {macArch === "arm64" ? "Apple Silicon" : "Intel"})
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
              ) : (
                <>
                  <Button
                    asChild
                    className="w-full"
                    variant={index === 0 ? "default" : "outline"}
                  >
                    <a href={downloadUrl(apiOrigin, "linux", "x64", linuxKind)}>
                      <Download className="mr-2 h-4 w-4" />
                      Download for Linux (
                      {linuxKind === "appimage" ? "AppImage" : ".deb"})
                    </a>
                  </Button>
                  <button
                    type="button"
                    onClick={() =>
                      setLinuxKind(
                        linuxKind === "appimage" ? "deb" : "appimage",
                      )
                    }
                    className="mx-auto block text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
                  >
                    {linuxKind === "appimage"
                      ? "Prefer a .deb package?"
                      : "Prefer the AppImage?"}
                  </button>
                  {/* Only x86-64 Linux assets are published. Saying so beats
                      handing an arm64 machine a build it cannot run. */}
                  {detectedArch === "arm64" && (
                    <p className="text-center text-xs text-muted-foreground">
                      Linux builds are x86-64 only for now.
                    </p>
                  )}
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
                of the desktop app failing silently against it. */}
            <div className="flex items-center gap-2 rounded-lg border bg-muted/50 p-2">
              <code className="min-w-0 flex-1 select-all break-all font-mono text-xs">
                {apiOrigin}
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
        </CardContent>
      </Card>
    </div>
  )
}
