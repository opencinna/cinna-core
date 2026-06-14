import { useGoogleLogin } from "@react-oauth/google"
import { useMutation } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { FcGoogle } from "react-icons/fc"
import type { GoogleCallbackRequest, LoginToken, MfaChallenge } from "@/client"
import { OauthService } from "@/client"
import { useMfaChallenge } from "@/components/Auth/MfaChallengeContext"
import { Button } from "@/components/ui/button"
import { isMfaChallengeResponse } from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { clearLoginScopedDisclaimerAck, safeRedirectPath } from "@/utils"
import { getTrustedDeviceToken } from "@/utils/trustedDevice"

const GOOGLE_REDIRECT_KEY = "google_oauth_redirect"

export function GoogleLoginButton() {
  const navigate = useNavigate()
  const { showErrorToast } = useCustomToast()
  const { setChallenge } = useMfaChallenge()
  const clientId = import.meta.env.VITE_GOOGLE_CLIENT_ID

  if (!clientId) return null

  const googleLoginMutation = useMutation<
    LoginToken | MfaChallenge,
    Error,
    string
  >({
    mutationFn: async (code: string) => {
      const state = sessionStorage.getItem("google_oauth_state") || ""
      const requestBody: GoogleCallbackRequest = {
        code,
        state,
        // "Do not ask on this device" — when a valid token is stored, the
        // backend skips the 2FA challenge and returns a token directly.
        trusted_device_token: getTrustedDeviceToken() ?? undefined,
      }
      return await OauthService.googleCallback({
        requestBody,
      })
    },
    onSuccess: (data) => {
      sessionStorage.removeItem("google_oauth_state")
      const stashed = sessionStorage.getItem(GOOGLE_REDIRECT_KEY)
      sessionStorage.removeItem(GOOGLE_REDIRECT_KEY)
      const target = safeRedirectPath(stashed)
      const safeTarget = target === "/" ? null : target

      if (isMfaChallengeResponse(data)) {
        setChallenge(data, safeTarget)
        void navigate({ to: "/login/mfa" })
        return
      }

      localStorage.setItem("access_token", data.access_token)
      clearLoginScopedDisclaimerAck()
      if (safeTarget) {
        window.location.assign(safeTarget)
        return
      }
      navigate({ to: "/" })
    },
    onError: (error: Error) => {
      sessionStorage.removeItem("google_oauth_state")
      sessionStorage.removeItem(GOOGLE_REDIRECT_KEY)
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
      // Stash any same-origin post-auth redirect target so we can honor it
      // after Google round-trips back to this page.
      try {
        const params = new URLSearchParams(window.location.search)
        const raw = params.get("redirect")
        const safe = safeRedirectPath(raw)
        if (safe !== "/") {
          sessionStorage.setItem(GOOGLE_REDIRECT_KEY, safe)
        } else {
          sessionStorage.removeItem(GOOGLE_REDIRECT_KEY)
        }
      } catch {
        sessionStorage.removeItem(GOOGLE_REDIRECT_KEY)
      }
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
