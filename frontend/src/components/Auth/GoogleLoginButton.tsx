import { useGoogleLogin } from "@react-oauth/google"
import { useMutation } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { FcGoogle } from "react-icons/fc"
import type { GoogleCallbackRequest } from "@/client"
import { OauthService } from "@/client"
import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"

export function GoogleLoginButton() {
  const navigate = useNavigate()
  const { showErrorToast } = useCustomToast()

  const googleLoginMutation = useMutation({
    mutationFn: async (code: string) => {
      const state = sessionStorage.getItem("google_oauth_state") || ""
      const requestBody: GoogleCallbackRequest = {
        code,
        state,
      }
      return await OauthService.googleCallback({
        requestBody,
      })
    },
    onSuccess: (data) => {
      localStorage.setItem("access_token", data.access_token)
      sessionStorage.removeItem("google_oauth_state")
      navigate({ to: "/" })
    },
    onError: (error: Error) => {
      sessionStorage.removeItem("google_oauth_state")
      showErrorToast(error.message || "Failed to login with Google")
    },
  })

  const handleGoogleLogin = useGoogleLogin({
    flow: "auth-code",
    onSuccess: (codeResponse) => {
      googleLoginMutation.mutate(codeResponse.code)
    },
    onError: () => {
      showErrorToast("Failed to login with Google")
    },
    state: (() => {
      const state = crypto.randomUUID()
      sessionStorage.setItem("google_oauth_state", state)
      return state
    })(),
  })

  return (
    <Button
      type="button"
      variant="outline"
      className="w-full"
      onClick={() => handleGoogleLogin()}
      disabled={googleLoginMutation.isPending}
    >
      <FcGoogle className="mr-2 h-5 w-5" />
      Continue with Google
    </Button>
  )
}
