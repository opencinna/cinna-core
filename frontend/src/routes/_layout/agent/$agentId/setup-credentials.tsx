/**
 * Setup-credentials page — installer fills in placeholder credentials.
 *
 * Reached from the SetupNeededBanner on the agent detail page (and from
 * MCP/A2A error renderings that include a ``setup_url``). One card per
 * placeholder credential the install owns; each card has a minimal
 * key=value editor that PUTs to ``/agents/{agentId}/setup-credentials/{credentialId}``.
 *
 * This is the v1 pragmatic UI — every credential type renders with the
 * generic key/value editor. A follow-up can plug in the per-type forms
 * from ``components/Credentials/CredentialForms``.
 *
 * TODO: per-type credential forms (Phase 4 follow-up)
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { ArrowLeft, CheckCircle2, Plus, Trash2 } from "lucide-react"
import { useState } from "react"

import {
  InstallsService,
  type SetupCredentialSummary,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute(
  "/_layout/agent/$agentId/setup-credentials",
)({
  component: SetupCredentialsPage,
})

function SetupCredentialsPage() {
  const { agentId } = Route.useParams()

  const {
    data: status,
    isLoading: statusLoading,
    error: statusError,
  } = useQuery({
    queryKey: ["agent", agentId, "setup-status"],
    queryFn: () => InstallsService.getSetupStatus({ agentId }),
    enabled: !!agentId,
  })

  const {
    data: credentials,
    isLoading: credsLoading,
    error: credsError,
  } = useQuery({
    queryKey: ["agent", agentId, "setup-credentials"],
    queryFn: () => InstallsService.listSetupCredentials({ agentId }),
    enabled: !!agentId,
  })

  const isLoading = statusLoading || credsLoading
  const accessError = statusError || credsError

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-3xl">
        <div className="mb-6 flex items-center gap-3">
          <Button asChild variant="ghost" size="sm">
            <Link to="/agent/$agentId" params={{ agentId }}>
              <ArrowLeft className="h-4 w-4" />
              Back to agent
            </Link>
          </Button>
        </div>

        <div className="mb-6">
          <h1 className="text-2xl font-semibold">Set up credentials</h1>
          <p className="text-sm text-muted-foreground">
            Fill in the credentials this agent needs before it can run.
          </p>
        </div>

        {isLoading && (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}

        {accessError && !isLoading && (
          <Card>
            <CardHeader>
              <CardTitle>Cannot access this install</CardTitle>
              <CardDescription>
                You may not own this install, or it no longer exists. Go back
                and pick a different agent.
              </CardDescription>
            </CardHeader>
          </Card>
        )}

        {!isLoading && !accessError && status?.status === "ready" && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <CheckCircle2 className="h-5 w-5 text-emerald-600" />
                Setup complete
              </CardTitle>
              <CardDescription>
                Setup complete — close this tab and return to your chat.
              </CardDescription>
            </CardHeader>
          </Card>
        )}

        {!isLoading &&
          !accessError &&
          status?.status === "publisher_broken" && (
            <Card>
              <CardHeader>
                <CardTitle>Publisher credentials unavailable</CardTitle>
                <CardDescription>
                  The publisher credentials for this install are no longer
                  available. Contact the publisher to restore access, or
                  replace them with your own credentials from the agent's
                  credentials tab.
                </CardDescription>
              </CardHeader>
            </Card>
          )}

        {!isLoading &&
          !accessError &&
          status?.status === "needs_setup" &&
          credentials &&
          credentials.length === 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Nothing to fill in here</CardTitle>
                <CardDescription>
                  This install needs setup, but the missing items aren't
                  installer-fillable credentials. Check the agent's credentials
                  tab for details.
                </CardDescription>
              </CardHeader>
            </Card>
          )}

        {!isLoading &&
          !accessError &&
          status?.status === "needs_setup" &&
          credentials &&
          credentials.length > 0 && (
            <div className="space-y-4">
              {credentials.map((cred) => (
                <SetupCredentialCard
                  key={cred.id}
                  agentId={agentId}
                  credential={cred}
                />
              ))}
            </div>
          )}
      </div>
    </div>
  )
}

interface SetupCredentialCardProps {
  agentId: string
  credential: SetupCredentialSummary
}

interface KeyValueRow {
  key: string
  value: string
}

function SetupCredentialCard({
  agentId,
  credential,
}: SetupCredentialCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [rows, setRows] = useState<KeyValueRow[]>([{ key: "", value: "" }])
  const [savedOnce, setSavedOnce] = useState(false)

  const addRow = () =>
    setRows((prev) => [...prev, { key: "", value: "" }])
  const removeRow = (index: number) =>
    setRows((prev) =>
      prev.length === 1 ? prev : prev.filter((_, i) => i !== index),
    )
  const updateRow = (index: number, patch: Partial<KeyValueRow>) =>
    setRows((prev) =>
      prev.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    )

  const buildCredentialData = (): Record<string, unknown> => {
    const data: Record<string, unknown> = {}
    for (const row of rows) {
      const key = row.key.trim()
      if (!key) continue
      data[key] = row.value
    }
    return data
  }

  const mutation = useMutation({
    mutationFn: () =>
      InstallsService.updateSetupCredential({
        agentId,
        credentialId: credential.id,
        requestBody: {
          credential_data: buildCredentialData(),
        },
      }),
    onSuccess: () => {
      setSavedOnce(true)
      showSuccessToast(`Saved ${credential.name}`)
      queryClient.invalidateQueries({
        queryKey: ["agent", agentId, "setup-status"],
      })
      queryClient.invalidateQueries({
        queryKey: ["agent", agentId, "setup-credentials"],
      })
    },
    onError: handleError.bind(showErrorToast),
  })

  const data = buildCredentialData()
  const canSave = Object.keys(data).length > 0 && !mutation.isPending

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <span className="truncate">{credential.name}</span>
              <Badge variant="secondary" className="shrink-0">
                {credential.type}
              </Badge>
            </CardTitle>
            {credential.description && (
              <CardDescription className="mt-1">
                {credential.description}
              </CardDescription>
            )}
          </div>
          {savedOnce && (
            <span className="inline-flex items-center gap-1 text-sm text-emerald-600 shrink-0">
              <CheckCircle2 className="h-4 w-4" />
              Saved
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Add the key/value pairs this credential expects (e.g.
          <code className="mx-1 rounded bg-muted px-1">api_key</code>).
        </p>
        <div className="space-y-2">
          {rows.map((row, i) => (
            <div key={i} className="flex items-end gap-2">
              <div className="flex-1">
                <Label
                  htmlFor={`${credential.id}-key-${i}`}
                  className="text-xs text-muted-foreground"
                >
                  Key
                </Label>
                <Input
                  id={`${credential.id}-key-${i}`}
                  value={row.key}
                  placeholder="api_key"
                  onChange={(e) => updateRow(i, { key: e.target.value })}
                />
              </div>
              <div className="flex-1">
                <Label
                  htmlFor={`${credential.id}-value-${i}`}
                  className="text-xs text-muted-foreground"
                >
                  Value
                </Label>
                <Input
                  id={`${credential.id}-value-${i}`}
                  value={row.value}
                  type="password"
                  onChange={(e) => updateRow(i, { value: e.target.value })}
                />
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => removeRow(i)}
                disabled={rows.length === 1}
                aria-label="Remove row"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
        <div className="flex items-center justify-between gap-2 pt-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={addRow}
          >
            <Plus className="h-4 w-4" />
            Add field
          </Button>
          <LoadingButton
            type="button"
            loading={mutation.isPending}
            disabled={!canSave}
            onClick={() => mutation.mutate()}
          >
            Save
          </LoadingButton>
        </div>
      </CardContent>
    </Card>
  )
}
