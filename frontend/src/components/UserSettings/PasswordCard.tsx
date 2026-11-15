import { KeyRound } from "lucide-react"

import ChangePassword from "@/components/UserSettings/ChangePassword"
import SetPassword from "@/components/UserSettings/SetPassword"
import SignOutEverywhere from "@/components/UserSettings/SignOutEverywhere"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import useAuth from "@/hooks/useAuth"

/**
 * Password and sessions (S3, P7): two facets of one concern. Changing the
 * password ends every other session; "Sign out everywhere" ends every
 * session, this one included, without a new password.
 */
export default function PasswordCard() {
  const { user } = useAuth()
  if (!user) return null
  const hasPassword = !!user.has_password

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <KeyRound className="h-5 w-5 shrink-0" />
          Password and sessions
        </CardTitle>
        <CardDescription>
          {hasPassword
            ? "Change your password or sign out every device signed in to your account."
            : "Set a password to sign in without Google, or sign out every device signed in to your account."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-5">
          <section className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <h3 className="font-medium text-sm">Password</h3>
              {hasPassword ? <ChangePassword /> : <SetPassword />}
            </div>
            <p className="text-sm text-muted-foreground">
              {hasPassword
                ? "Changing it signs out every other browser and app."
                : "Setting one signs out every other browser and app."}
            </p>
          </section>
          <Separator />
          <section className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <h3 className="font-medium text-sm">Sign out everywhere</h3>
              <SignOutEverywhere />
            </div>
            <p className="text-sm text-muted-foreground">
              End every session on every device, including this one.
            </p>
          </section>
        </div>
      </CardContent>
    </Card>
  )
}
