import { createFileRoute, redirect } from "@tanstack/react-router"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useState, useEffect } from "react"
import { z } from "zod"
import { isLoggedIn } from "@/hooks/useAuth"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  Loader2,
  ShieldCheck,
  ShieldX,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react"

const searchSchema = z.object({
  nonce: z.string(),
})

export const Route = createFileRoute("/oauth/mcp-consent")({
  component: McpConsentPage,
  validateSearch: searchSchema,
  beforeLoad: async ({ search }) => {
    if (!isLoggedIn()) {
      // Note: The login page does not currently support a redirect search param.
      // The user will need to re-navigate to the consent URL after logging in,
      // or the OAuth client will need to restart the authorization flow.
      throw redirect({
        to: "/login",
        search: { redirect: `/oauth/mcp-consent?nonce=${search.nonce}` },
      })
    }
  },
  head: () => ({
    meta: [{ title: "Authorize Access - Workflow Runner" }],
  }),
})

const API_BASE = import.meta.env.VITE_API_URL || ""

async function fetchConsentInfo(nonce: string) {
  const res = await fetch(`${API_BASE}/api/v1/mcp/consent/${nonce}`)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Error ${res.status}`)
  }
  return res.json()
}

async function approveConsent(nonce: string) {
  const token = localStorage.getItem("access_token")
  const res = await fetch(`${API_BASE}/api/v1/mcp/consent/${nonce}/approve`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Error ${res.status}`)
  }
  return res.json()
}

function McpConsentPage() {
  const { nonce } = Route.useSearch()
  const [authorized, setAuthorized] = useState(false)
  const [denied, setDenied] = useState(false)

  const {
    data: consentInfo,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["mcp-consent", nonce],
    queryFn: () => fetchConsentInfo(nonce),
  })

  const approveMutation = useMutation({
    mutationFn: () => approveConsent(nonce),
    onSuccess: (data: { redirect_url: string }) => {
      setAuthorized(true)
      window.location.href = data.redirect_url
    },
  })

  // After authorization or denial, try to close the tab.
  // Wait long enough for the browser to dispatch the redirect URL
  // (especially custom protocol URLs like claude-desktop://) before
  // attempting to close. window.close() only works for script-opened
  // tabs; if it fails, the user sees a fallback message.
  useEffect(() => {
    if (!authorized && !denied) return
    const timer = setTimeout(() => {
      window.close()
    }, 10000)
    return () => clearTimeout(timer)
  }, [authorized, denied])

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (authorized) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <CheckCircle2 className="mx-auto h-12 w-12 text-green-500" />
            <CardTitle className="mt-2">Authorization Successful</CardTitle>
            <CardDescription>
              You can close this tab and return to the application.
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  if (denied) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <ShieldX className="mx-auto h-12 w-12 text-muted-foreground" />
            <CardTitle className="mt-2">Authorization Denied</CardTitle>
            <CardDescription>
              You can close this tab.
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <AlertTriangle className="mx-auto h-12 w-12 text-destructive" />
            <CardTitle className="mt-2">Authorization Error</CardTitle>
            <CardDescription>{(error as Error).message}</CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  const handleDeny = () => {
    if (consentInfo?.redirect_uri) {
      const url = new URL(consentInfo.redirect_uri)
      url.searchParams.set("error", "access_denied")
      if (consentInfo.state) {
        url.searchParams.set("state", consentInfo.state)
      }
      setDenied(true)
      window.location.href = url.toString()
    } else {
      window.close()
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <ShieldCheck className="mx-auto h-12 w-12 text-primary" />
          <CardTitle className="mt-2">Authorize Access</CardTitle>
          <CardDescription>
            An application wants to connect to your agent
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="rounded-lg border p-4 space-y-3">
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Agent</span>
              <span className="text-sm font-medium">
                {consentInfo.agent_name}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Connector</span>
              <span className="text-sm font-medium">
                {consentInfo.connector_name}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Mode</span>
              <Badge variant="secondary">{consentInfo.connector_mode}</Badge>
            </div>
            <div className="flex justify-between">
              <span className="text-sm text-muted-foreground">Application</span>
              <span className="text-sm font-medium">
                {consentInfo.client_name || "Unknown Client"}
              </span>
            </div>
            {consentInfo.scopes?.length > 0 && (
              <div>
                <span className="text-sm text-muted-foreground">
                  Permissions
                </span>
                <div className="mt-1 flex flex-wrap gap-1">
                  {consentInfo.scopes.map((scope: string) => (
                    <Badge key={scope} variant="outline">
                      {scope}
                    </Badge>
                  ))}
                </div>
              </div>
            )}
          </div>
        </CardContent>
        <CardFooter className="flex gap-3">
          <Button
            variant="outline"
            className="flex-1"
            onClick={handleDeny}
            disabled={approveMutation.isPending}
          >
            <ShieldX className="mr-2 h-4 w-4" />
            Deny
          </Button>
          <Button
            className="flex-1"
            onClick={() => approveMutation.mutate()}
            disabled={approveMutation.isPending}
          >
            {approveMutation.isPending ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <ShieldCheck className="mr-2 h-4 w-4" />
            )}
            Authorize
          </Button>
        </CardFooter>
        {approveMutation.isError && (
          <div className="px-6 pb-4">
            <p className="text-sm text-destructive text-center">
              {(approveMutation.error as Error).message}
            </p>
          </div>
        )}
      </Card>
    </div>
  )
}
