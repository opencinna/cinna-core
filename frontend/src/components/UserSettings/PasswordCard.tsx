import { KeyRound } from "lucide-react"

import ChangePassword from "@/components/UserSettings/ChangePassword"
import SetPassword from "@/components/UserSettings/SetPassword"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useAuth from "@/hooks/useAuth"

export default function PasswordCard() {
  const { user } = useAuth()
  if (!user) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-blue-500" />
          Password
        </CardTitle>
        <CardDescription>
          {user.has_password
            ? "Change the password used to sign in to your account."
            : "Set a password to sign in without Google."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {user.has_password ? <ChangePassword /> : <SetPassword />}
      </CardContent>
    </Card>
  )
}
