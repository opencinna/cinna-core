import { useGoogleLogin } from "@react-oauth/google"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { FcGoogle } from "react-icons/fc"
import type { GoogleCallbackRequest } from "@/client"
import { OauthService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"

export default function OAuthAccounts() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const unlinkMutation = useMutation({
    mutationFn: () => OauthService.unlinkGoogleAccountEndpoint(),
    onSuccess: () => {
      showSuccessToast("Google account unlinked successfully")
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
    onError: (error: Error) => {
      showErrorToast(error.message || "Failed to unlink Google account")
    },
  })

  const linkMutation = useMutation({
    mutationFn: async (code: string) => {
      const state = sessionStorage.getItem("google_oauth_state") || ""
      const requestBody: GoogleCallbackRequest = {
        code,
        state,
      }
      return OauthService.linkGoogleAccountEndpoint({ requestBody })
    },
    onSuccess: () => {
      showSuccessToast("Google account linked successfully")
      sessionStorage.removeItem("google_oauth_state")
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
    onError: (error: Error) => {
      sessionStorage.removeItem("google_oauth_state")
      showErrorToast(error.message || "Failed to link Google account")
    },
  })

  const handleGoogleLink = useGoogleLogin({
    flow: "auth-code",
    onSuccess: (codeResponse) => {
      linkMutation.mutate(codeResponse.code)
    },
    onError: () => {
      showErrorToast("Failed to link Google account")
    },
    state: (() => {
      const state = crypto.randomUUID()
      sessionStorage.setItem("google_oauth_state", state)
      return state
    })(),
  })

  if (!user) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Connected Accounts</CardTitle>
        <CardDescription>
          Manage your OAuth account connections
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <FcGoogle className="h-6 w-6" />
            <div>
              <p className="font-medium">Google</p>
              <p className="text-sm text-muted-foreground">
                {user.has_google_account ? "Connected" : "Not connected"}
              </p>
            </div>
          </div>

          {user.has_google_account ? (
            <Button
              variant="destructive"
              size="sm"
              onClick={() => unlinkMutation.mutate()}
              disabled={unlinkMutation.isPending || !user.has_password}
            >
              Disconnect
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleGoogleLink()}
              disabled={linkMutation.isPending}
            >
              Connect
            </Button>
          )}
        </div>

        {user.has_google_account && !user.has_password && (
          <p className="text-sm text-amber-600 dark:text-amber-400">
            You must set a password before disconnecting Google
          </p>
        )}
      </CardContent>
    </Card>
  )
}
