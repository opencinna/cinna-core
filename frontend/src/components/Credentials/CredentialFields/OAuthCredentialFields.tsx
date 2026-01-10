import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { RefreshCw, CheckCircle2, ChevronDown, ChevronUp, AlertTriangle } from "lucide-react"
import { useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { CredentialsService, type OAuthMetadataResponse } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { RelativeTime } from "@/components/Common/RelativeTime"

interface OAuthCredentialFieldsProps {
  credentialType: string
  credentialId?: string
}

// Map credential type to display name
const CREDENTIAL_TYPE_NAMES: Record<string, string> = {
  gmail_oauth: "Gmail",
  gmail_oauth_readonly: "Gmail (Read-Only)",
  gdrive_oauth: "Google Drive",
  gdrive_oauth_readonly: "Google Drive (Read-Only)",
  gcalendar_oauth: "Google Calendar",
  gcalendar_oauth_readonly: "Google Calendar (Read-Only)",
}

export function OAuthCredentialFields({
  credentialType,
  credentialId,
}: OAuthCredentialFieldsProps) {
  const displayName = CREDENTIAL_TYPE_NAMES[credentialType] || "Google Service"
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [showScopes, setShowScopes] = useState(false)

  // Fetch OAuth metadata if credential already exists
  const { data: metadata, isLoading: isLoadingMetadata } = useQuery<OAuthMetadataResponse | null>({
    queryKey: ["oauthMetadata", credentialId],
    queryFn: async () => {
      if (!credentialId) return null
      return await CredentialsService.getOauthMetadata({ credentialId })
    },
    enabled: !!credentialId,
    refetchInterval: false,
  })

  // Mutation to initiate OAuth flow
  const authorizeMutation = useMutation({
    mutationFn: () =>
      CredentialsService.oauthAuthorize({ credentialId: credentialId! }),
    onSuccess: (data) => {
      // Open authorization URL in popup or redirect
      const authUrl = data.authorization_url
      const width = 600
      const height = 700
      const left = window.screenX + (window.outerWidth - width) / 2
      const top = window.screenY + (window.outerHeight - height) / 2

      const popup = window.open(
        authUrl,
        "Google OAuth",
        `width=${width},height=${height},left=${left},top=${top}`
      )

      if (!popup) {
        showErrorToast("Failed to open authorization window. Please allow popups.")
        return
      }

      // Poll for popup closure or success
      const checkPopup = setInterval(() => {
        if (popup.closed) {
          clearInterval(checkPopup)
          // Refresh metadata after OAuth flow completes
          queryClient.invalidateQueries({ queryKey: ["oauthMetadata", credentialId] })
          queryClient.invalidateQueries({ queryKey: ["credentials"] })
          queryClient.invalidateQueries({ queryKey: ["credential", credentialId] })
        }
      }, 500)
    },
    onError: handleError.bind(showErrorToast),
  })

  // Mutation to refresh OAuth token
  const refreshMutation = useMutation({
    mutationFn: () =>
      CredentialsService.refreshOauthToken({ credentialId: credentialId! }),
    onSuccess: () => {
      showSuccessToast("OAuth token refreshed successfully")
      queryClient.invalidateQueries({ queryKey: ["oauthMetadata", credentialId] })
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credentialId] })
    },
    onError: handleError.bind(showErrorToast),
  })

  const handleGrantFromGoogle = () => {
    if (!credentialId) {
      showErrorToast("Please save the credential first before authorizing")
      return
    }
    authorizeMutation.mutate()
  }

  const handleRefreshToken = () => {
    if (!credentialId) {
      return
    }
    refreshMutation.mutate()
  }

  const isAuthorized = metadata && metadata.user_email
  const isExpired = metadata?.expires_at ? metadata.expires_at * 1000 < Date.now() : false

  return (
    <Card>
      <CardHeader>
        <CardTitle>OAuth Authorization</CardTitle>
        <CardDescription>
          Grant access to your {displayName} account
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!credentialId ? (
          // Show message if credential not saved yet
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <p className="text-sm text-muted-foreground">
              Please save the credential first to authorize with Google.
            </p>
          </div>
        ) : isLoadingMetadata ? (
          // Loading state
          <div className="flex items-center justify-center py-12">
            <div className="text-sm text-muted-foreground">Loading authorization status...</div>
          </div>
        ) : !isAuthorized ? (
          // Not authorized - show centered grant button
          <div className="flex flex-col items-center justify-center py-12 space-y-4">
            <div className="text-center space-y-2">
              <p className="text-sm text-muted-foreground">
                No authorization found. Click below to grant access.
              </p>
            </div>
            <Button
              type="button"
              size="lg"
              onClick={handleGrantFromGoogle}
              disabled={authorizeMutation.isPending}
            >
              {authorizeMutation.isPending
                ? "Opening authorization..."
                : "Grant from Google"}
            </Button>
          </div>
        ) : (
          // Authorized - show metadata and actions
          <div className="space-y-4">
            {/* Authorization Status */}
            <div className="flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5 text-green-500" />
              <span className="font-medium">Authorised</span>
            </div>

            {/* Account Email - Inline */}
            <div className="flex items-baseline gap-2">
              <span className="text-sm text-muted-foreground">Account:</span>
              <span className="text-sm font-medium break-all">{metadata.user_email}</span>
            </div>

            {/* Expiration - Inline */}
            {metadata.expires_at && (
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">Expires:</span>
                <span
                  className="text-sm font-medium"
                  title={new Date(metadata.expires_at * 1000).toLocaleString()}
                >
                  <RelativeTime
                    timestamp={new Date(metadata.expires_at * 1000)}
                    fallback="soon"
                  />
                </span>
                {isExpired && (
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-500">
                    <AlertTriangle className="h-3 w-3" />
                    Expired
                  </span>
                )}
              </div>
            )}

            {/* Collapsible Scopes */}
            {metadata.scopes && metadata.scopes.length > 0 && (
              <div className="border-t pt-4">
                <button
                  type="button"
                  onClick={() => setShowScopes(!showScopes)}
                  className="flex items-center justify-between w-full text-sm text-muted-foreground hover:text-foreground transition-colors"
                >
                  <span>Granted Scopes ({metadata.scopes.length})</span>
                  {showScopes ? (
                    <ChevronUp className="h-4 w-4" />
                  ) : (
                    <ChevronDown className="h-4 w-4" />
                  )}
                </button>
                {showScopes && (
                  <div className="mt-2 rounded-lg border p-3 bg-muted/30">
                    <ul className="space-y-1 text-xs text-muted-foreground">
                      {metadata.scopes.map((scope, i) => (
                        <li key={i} className="break-all">
                          • {scope}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {/* Action Buttons */}
            <div className="flex gap-2 pt-4 border-t">
              <Button
                type="button"
                variant="outline"
                onClick={handleGrantFromGoogle}
                disabled={authorizeMutation.isPending}
                className="flex-1"
              >
                {authorizeMutation.isPending
                  ? "Opening authorization..."
                  : "Re-authorize"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={handleRefreshToken}
                disabled={refreshMutation.isPending}
                className="flex-1"
              >
                <RefreshCw className="mr-2 h-4 w-4" />
                {refreshMutation.isPending ? "Refreshing..." : "Refresh Token"}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
