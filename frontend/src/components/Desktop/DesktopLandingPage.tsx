import { Monitor } from "lucide-react"

import { DesktopDownloadSection } from "@/components/Desktop/DesktopDownloadSection"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

/**
 * Public onboarding page for Cinna Desktop, linked from the new-account email.
 *
 * The page shell only: the offer itself lives in `DesktopDownloadSection`, so
 * `/start` can show the same download flow inside one card among several
 * without inheriting this page's full-viewport centring and its own heading.
 * What `/desktop` renders is unchanged — old emails point here.
 */
export function DesktopLandingPage() {
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
        <CardContent>
          <DesktopDownloadSection />
        </CardContent>
      </Card>
    </div>
  )
}
