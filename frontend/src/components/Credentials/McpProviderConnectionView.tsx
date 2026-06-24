import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Check,
  CheckCircle2,
  Copy,
  KeyRound,
  Plug,
  RefreshCw,
  Server,
  XCircle,
} from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import type { CredentialWithData } from "@/client"
import { CredentialsService, McpProvidersService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import { AgentBadge } from "@/components/Common/AgentBadge"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { openMcpProviderOAuthPopup } from "@/utils/mcpProviderOAuth"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

const STATUS_META: Record<string, { label: string; className: string }> = {
  connected: {
    label: "Connected",
    className:
      "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-200 dark:border-emerald-900",
  },
  awaiting_auth: {
    label: "Authorization required",
    className:
      "bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-900",
  },
  expired: {
    label: "Token expired",
    className:
      "bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-900",
  },
  error: {
    label: "Error",
    className:
      "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-200 dark:border-red-900",
  },
}

const AUTH_MODE_LABEL: Record<string, string> = {
  agent2agent: "Platform agent",
  fixed_token: "Fixed token",
  oauth_dcr: "OAuth (DCR)",
  none: "No auth",
}

/**
 * Detail view for an ``mcp_provider`` credential — the record that connects a
 * consumer agent to another MCP server (a platform agent's agent-to-agent
 * connector or an arbitrary external server). The token/secrets are managed
 * internally (never shown or edited); this view surfaces the connection
 * (endpoint, transport, auth mode, target agent, lifecycle status), the
 * per-mode applicability toggles, and Test / Reauthorize actions. Deleting the
 * credential disconnects this consumer.
 */
export function McpProviderConnectionView({
  credential,
}: {
  credential: CredentialWithData
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copied, setCopied] = useState(false)

  const { data: status, isLoading: statusLoading } = useQuery({
    queryKey: ["mcp-provider-status", credential.id],
    queryFn: () =>
      McpProvidersService.getProviderStatus({ credentialId: credential.id }),
  })

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: { name: credential.name, notes: credential.notes ?? "" },
  })

  useEffect(() => {
    form.reset({ name: credential.name, notes: credential.notes ?? "" })
  }, [credential, form])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["credentials"] })
    queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
    queryClient.invalidateQueries({
      queryKey: ["credential-with-data", credential.id],
    })
    queryClient.invalidateQueries({
      queryKey: ["mcp-provider-status", credential.id],
    })
  }

  // Name / notes save (metadata only).
  const updateMutation = useMutation({
    mutationFn: (data: FormData) =>
      CredentialsService.updateCredential({
        id: credential.id,
        requestBody: data,
      }),
    onSuccess: () => showSuccessToast("Connection updated"),
    onError: handleError.bind(showErrorToast),
    onSettled: invalidate,
  })

  // Per-mode applicability toggles live on the credential row.
  const modeMutation = useMutation({
    mutationFn: (body: {
      mcp_mode_conversation?: boolean
      mcp_mode_building?: boolean
    }) =>
      CredentialsService.updateCredential({
        id: credential.id,
        requestBody: body,
      }),
    onSuccess: () => showSuccessToast("Modes updated"),
    onError: handleError.bind(showErrorToast),
    onSettled: invalidate,
  })

  const testMutation = useMutation({
    mutationFn: () =>
      McpProvidersService.testConnection({ credentialId: credential.id }),
    onSuccess: (res) => {
      if (res.ok) {
        showSuccessToast(
          res.tools && res.tools.length > 0
            ? `Connection OK — ${res.tools.length} tool(s) available`
            : "Connection OK",
        )
      } else {
        showErrorToast(res.error || "Connection test failed")
      }
    },
    onError: handleError.bind(showErrorToast),
    onSettled: invalidate,
  })

  const authorizeMutation = useMutation({
    mutationFn: () =>
      McpProvidersService.oauthReauthorize({ credentialId: credential.id }),
    onSuccess: (res) => {
      openMcpProviderOAuthPopup({
        authorizeUrl: res.authorize_url,
        onSuccess: () => {
          showSuccessToast("MCP server authorized")
          invalidate()
        },
        onError: (msg) => showErrorToast(msg || "Authorization failed"),
      })
    },
    onError: handleError.bind(showErrorToast),
  })

  const handleCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Failed to copy")
    }
  }

  const data = credential.credential_data as Record<string, unknown> | null
  const endpointUrl =
    status?.endpoint_url || (data?.endpoint_url as string | undefined) || ""
  const authMode =
    status?.auth_mode || (data?.auth_mode as string | undefined) || "none"
  const targetAgent = status?.target_agent ?? null
  const isOAuth = authMode === "oauth_dcr"
  const statusKey = status?.status ?? "connected"
  const statusMeta = STATUS_META[statusKey] ?? STATUS_META.connected

  const modeConversation = credential.mcp_mode_conversation ?? true
  const modeBuilding = credential.mcp_mode_building ?? true

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Plug className="h-5 w-5" />
          MCP provider connection
        </CardTitle>
        <CardDescription>
          This credential connects an agent to an MCP server. It is injected
          into the agent's SDK as a first-class MCP server — delete this
          credential to disconnect.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left column: editable label / notes */}
          <Form {...form}>
            <form
              onSubmit={form.handleSubmit((d) => updateMutation.mutate(d))}
              className="space-y-4"
            >
              <FormField
                control={form.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      Name <span className="text-destructive">*</span>
                    </FormLabel>
                    <FormControl>
                      <Input
                        placeholder="Connection name"
                        type="text"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="notes"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Notes</FormLabel>
                    <FormControl>
                      <Textarea
                        placeholder="Additional notes..."
                        className="min-h-[120px]"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              {form.formState.isDirty && (
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => form.reset()}
                    disabled={updateMutation.isPending}
                  >
                    Reset
                  </Button>
                  <LoadingButton
                    type="submit"
                    loading={updateMutation.isPending}
                  >
                    Save Changes
                  </LoadingButton>
                </div>
              )}
            </form>
          </Form>

          {/* Right column: connection summary + per-mode applicability */}
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-muted-foreground">
                Connects to
              </span>
              {targetAgent ? (
                <AgentBadge agent={targetAgent} linkTo="agent" />
              ) : (
                <Badge variant="secondary" className="gap-1">
                  <Server className="h-3 w-3" />
                  External server
                </Badge>
              )}
              <Badge variant="outline" className="gap-1 text-xs">
                <KeyRound className="h-3 w-3" />
                {AUTH_MODE_LABEL[authMode] ?? authMode}
              </Badge>
              {!statusLoading && (
                <Badge
                  variant="outline"
                  className={`gap-1 text-xs ${statusMeta.className}`}
                >
                  {statusKey === "connected" ? (
                    <CheckCircle2 className="h-3 w-3" />
                  ) : statusKey === "error" ? (
                    <XCircle className="h-3 w-3" />
                  ) : null}
                  {statusMeta.label}
                </Badge>
              )}
            </div>

            {endpointUrl && (
              <div className="flex items-center gap-2">
                <code className="text-xs text-muted-foreground truncate flex-1">
                  {endpointUrl}
                </code>
                <Button
                  variant="outline"
                  size="icon"
                  className="shrink-0 h-7 w-7"
                  onClick={() => handleCopy(endpointUrl)}
                  title="Copy endpoint URL"
                >
                  {copied ? (
                    <Check className="h-3.5 w-3.5 text-green-500" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                </Button>
              </div>
            )}

            {status?.last_error && (
              <p className="text-xs text-red-600 dark:text-red-400">
                {status.last_error}
              </p>
            )}

            <div className="flex items-center gap-2">
              <LoadingButton
                variant="outline"
                size="sm"
                loading={testMutation.isPending}
                onClick={() => testMutation.mutate()}
              >
                <RefreshCw className="h-4 w-4 mr-1" />
                Test
              </LoadingButton>
              {isOAuth && (
                <LoadingButton
                  variant="outline"
                  size="sm"
                  loading={authorizeMutation.isPending}
                  onClick={() => authorizeMutation.mutate()}
                >
                  <KeyRound className="h-4 w-4 mr-1" />
                  {statusKey === "awaiting_auth" ? "Authorize" : "Reauthorize"}
                </LoadingButton>
              )}
            </div>

            {/* Per-mode applicability */}
            <div className="space-y-2 border-t pt-4">
              <Label>Apply to modes</Label>
              <div className="flex items-center gap-6">
                <label className="flex items-center gap-2 text-sm">
                  <Switch
                    checked={modeConversation}
                    disabled={modeMutation.isPending}
                    onCheckedChange={(c) =>
                      modeMutation.mutate({ mcp_mode_conversation: c })
                    }
                  />
                  Conversation
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <Switch
                    checked={modeBuilding}
                    disabled={modeMutation.isPending}
                    onCheckedChange={(c) =>
                      modeMutation.mutate({ mcp_mode_building: c })
                    }
                  />
                  Building
                </label>
              </div>
              {!modeConversation && !modeBuilding && (
                <p className="text-xs text-amber-600 dark:text-amber-400">
                  Both modes are off — this provider is currently inert and will
                  not be injected into any session.
                </p>
              )}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
