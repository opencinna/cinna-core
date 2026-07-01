import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowRight,
  Bot,
  Check,
  CheckCircle2,
  Copy,
  KeyRound,
  Loader2,
  MonitorSmartphone,
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
import { cn } from "@/lib/utils"
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
 * Read-only badge mirroring a per-mode toggle on the server side of the schema —
 * an *indicator* of which modes the connection is live in (the client owns the
 * editable switches; the server box reflects the resulting reachability).
 */
function ModeIndicator({ label, active }: { label: string; active: boolean }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1 text-xs",
        active
          ? "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-200 dark:border-emerald-900"
          : "text-muted-foreground/60 line-through",
      )}
    >
      {active && <Check className="h-3 w-3" />}
      {label}
    </Badge>
  )
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
  // (Fix 2) The consumer side of an agent2agent pair, when bound. Null for
  // external/manual providers and for unbound (floating) connections.
  const consumerAgent = status?.consumer_agent ?? null
  const isOAuth = authMode === "oauth_dcr"
  const statusKey = status?.status ?? "connected"
  const statusMeta = STATUS_META[statusKey] ?? STATUS_META.connected

  const modeConversation = credential.mcp_mode_conversation ?? true
  const modeBuilding = credential.mcp_mode_building ?? true

  // Server-side reachability — only an agent2agent connection has a mode: its
  // producer connector serves exactly one mode (`connector_mode`), regardless
  // of the consumer's toggles. External servers have no notion of modes, so the
  // server-side block is omitted entirely (connectorMode is null).
  const connectorMode = status?.connector_mode ?? null
  const serverInConversation = connectorMode === "conversation"
  const serverInBuilding = connectorMode === "building"

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
      <CardContent className="space-y-6">
        {/*
          Connection schema — reads left-to-right as the data actually flows:
          the MCP **Client** (the consumer agent using this connection) calls
          tools on the MCP **Server** (the producer agent / external server that
          exposes them). The client owns the editable per-mode switches; the
          server box mirrors them as read-only reachability indicators.
        */}
        <div className="rounded-lg border bg-muted/30 p-4 sm:p-5">
          <div className="flex flex-col md:flex-row items-stretch gap-3">
            {/* MCP Client — the consumer agent that uses this connection */}
            <div className="flex-1 space-y-3 rounded-md border bg-background p-4">
              <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                <MonitorSmartphone className="h-3.5 w-3.5" />
                MCP Client
              </div>
              {consumerAgent ? (
                <AgentBadge agent={consumerAgent} linkTo="agent" />
              ) : (
                <Badge
                  variant="outline"
                  className="gap-1 border-dashed text-muted-foreground"
                >
                  <Bot className="h-3 w-3" />
                  Any linked agent
                </Badge>
              )}
              <p className="text-xs text-muted-foreground">
                Calls the tools exposed by the MCP server.
              </p>
              {/* Editable: the modes in which this client uses the connection. */}
              <div className="space-y-2 border-t pt-3">
                <Label className="text-xs">Enabled in modes</Label>
                <div className="flex flex-col gap-2">
                  <label className="flex items-center justify-between gap-2 text-sm">
                    <span>Conversation</span>
                    <Switch
                      checked={modeConversation}
                      disabled={modeMutation.isPending}
                      onCheckedChange={(c) =>
                        modeMutation.mutate({ mcp_mode_conversation: c })
                      }
                    />
                  </label>
                  <label className="flex items-center justify-between gap-2 text-sm">
                    <span>Building</span>
                    <Switch
                      checked={modeBuilding}
                      disabled={modeMutation.isPending}
                      onCheckedChange={(c) =>
                        modeMutation.mutate({ mcp_mode_building: c })
                      }
                    />
                  </label>
                </div>
                {!modeConversation && !modeBuilding && (
                  <p className="text-xs text-amber-600 dark:text-amber-400">
                    Both modes are off — this connection is inert and will not be
                    injected into any session.
                  </p>
                )}
              </div>
            </div>

            {/* Connector — points client → server (down on mobile). */}
            <div className="flex flex-row md:flex-col items-center justify-center gap-1.5">
              <div className="h-px w-6 bg-border md:h-6 md:w-px" />
              <div className="rounded-full border bg-background p-1.5 text-muted-foreground shadow-sm">
                <ArrowRight className="h-4 w-4 rotate-90 md:rotate-0" />
              </div>
              <div className="h-px w-6 bg-border md:h-6 md:w-px" />
            </div>

            {/* MCP Server — the producer agent / external server. */}
            <div className="flex-1 space-y-3 rounded-md border bg-background p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  <Server className="h-3.5 w-3.5" />
                  MCP Server
                </div>
                <div className="flex items-center gap-1">
                  <Button
                    variant="outline"
                    size="icon"
                    className="h-7 w-7"
                    title="Test connection"
                    disabled={testMutation.isPending}
                    onClick={() => testMutation.mutate()}
                  >
                    {testMutation.isPending ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RefreshCw className="h-3.5 w-3.5" />
                    )}
                  </Button>
                  {isOAuth && (
                    <Button
                      variant="outline"
                      size="icon"
                      className="h-7 w-7"
                      title={
                        statusKey === "awaiting_auth"
                          ? "Authorize"
                          : "Reauthorize"
                      }
                      disabled={authorizeMutation.isPending}
                      onClick={() => authorizeMutation.mutate()}
                    >
                      {authorizeMutation.isPending ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <KeyRound className="h-3.5 w-3.5" />
                      )}
                    </Button>
                  )}
                </div>
              </div>
              {targetAgent ? (
                <AgentBadge agent={targetAgent} linkTo="agent" />
              ) : (
                <Badge variant="secondary" className="gap-1">
                  <Server className="h-3 w-3" />
                  External server
                </Badge>
              )}
              <div className="flex flex-wrap items-center gap-1.5">
                {/* Auth mode is only meaningful for external servers; for an
                    agent2agent connection ("Platform agent") it is already
                    obvious from the agent badge above. */}
                {authMode !== "agent2agent" && (
                  <Badge variant="outline" className="gap-1 text-xs">
                    <KeyRound className="h-3 w-3" />
                    {AUTH_MODE_LABEL[authMode] ?? authMode}
                  </Badge>
                )}
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
              {/* Read-only: the mode the producer connector actually serves.
                  Only an agent2agent server has a mode — an external MCP server
                  has no notion of modes, so the block is omitted entirely. */}
              {connectorMode && (
                <div className="space-y-1.5 border-t pt-3">
                  <span className="text-xs text-muted-foreground">
                    Serves mode
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    <ModeIndicator
                      label="Conversation"
                      active={serverInConversation}
                    />
                    <ModeIndicator label="Building" active={serverInBuilding} />
                  </div>
                </div>
              )}
              {endpointUrl && (
                <div className="flex items-center gap-2 border-t pt-3">
                  <code className="flex-1 truncate text-[11px] text-muted-foreground">
                    {endpointUrl}
                  </code>
                  <Button
                    variant="outline"
                    size="icon"
                    className="h-7 w-7 shrink-0"
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
            </div>
          </div>

          {status?.last_error && (
            <p className="mt-3 text-xs text-red-600 dark:text-red-400">
              {status.last_error}
            </p>
          )}
        </div>

        {/* Editable label / notes (metadata only). */}
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
                      className="min-h-[100px]"
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
                <LoadingButton type="submit" loading={updateMutation.isPending}>
                  Save Changes
                </LoadingButton>
              </div>
            )}
          </form>
        </Form>
      </CardContent>
    </Card>
  )
}
