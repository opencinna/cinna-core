import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FileJson, Lock, Network } from "lucide-react"
import { useEffect } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import type { CredentialWithData } from "@/client"
import { CredentialsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import { AgentBadge } from "@/components/Common/AgentBadge"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { openAgentApiSpec } from "@/utils/agentApiSpec"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

/**
 * Detail view for an ``agent_api`` credential — the record that connects a
 * consumer agent to a producer agent's REST API. The proxy token is managed
 * internally (never shown or edited); this view surfaces the connection itself:
 * which producer it proxies, which consumer agents are wired to it, and a
 * "View Spec" shortcut. Deleting the credential disconnects the agents.
 */
export function AgentApiConnectionView({
  credential,
}: {
  credential: CredentialWithData
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: connection, isLoading: connLoading } = useQuery({
    queryKey: ["agentApiConnection", credential.id],
    queryFn: () =>
      CredentialsService.readAgentApiConnection({ id: credential.id }),
  })

  const producerAgentId = connection?.producer_agent_id ?? undefined

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: { name: credential.name, notes: credential.notes ?? "" },
  })

  useEffect(() => {
    form.reset({ name: credential.name, notes: credential.notes ?? "" })
  }, [credential, form])

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      CredentialsService.updateCredential({
        id: credential.id,
        requestBody: data,
      }),
    onSuccess: () => showSuccessToast("Connection updated"),
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
      queryClient.invalidateQueries({
        queryKey: ["credential-with-data", credential.id],
      })
    },
  })

  const producerName =
    connection?.producer_agent_name ||
    (credential.credential_data?.label as string | undefined) ||
    "Producer agent"
  const consumers = connection?.consumer_agents ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Network className="h-5 w-5" />
          Agent REST API connection
        </CardTitle>
        <CardDescription>
          This credential connects an agent to another agent's REST API. The
          access token is managed for you — delete this credential to
          disconnect.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left column: editable label / notes */}
          <Form {...form}>
            <form
              onSubmit={form.handleSubmit((d) => mutation.mutate(d))}
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
                    disabled={mutation.isPending}
                  >
                    Reset
                  </Button>
                  <LoadingButton type="submit" loading={mutation.isPending}>
                    Save Changes
                  </LoadingButton>
                </div>
              )}
            </form>
          </Form>

          {/* Right column: producer info + connected-agents list */}
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-muted-foreground">
                Producer
              </span>
              <AgentBadge
                agent={{
                  id: producerAgentId ?? "",
                  name: producerName,
                  ui_color_preset: connection?.producer_ui_color_preset,
                }}
                linkTo={producerAgentId ? "agent" : "none"}
              />
              {connection?.read_only && (
                <Badge variant="outline" className="gap-1 text-xs">
                  <Lock className="h-3 w-3" />
                  read-only
                </Badge>
              )}
              <Button
                variant="outline"
                size="sm"
                className="ml-auto"
                disabled={!producerAgentId}
                onClick={() =>
                  producerAgentId && openAgentApiSpec(producerAgentId)
                }
                title={
                  producerAgentId
                    ? "Open the endpoints this connection exposes (rendered docs) in a new tab"
                    : "Producer agent is no longer accessible"
                }
              >
                <FileJson className="h-4 w-4 mr-1" />
                View Spec
              </Button>
            </div>

            {/* Connected consumer agents — name + owner (disambiguates
                identical agent names across bundle installs by owner). */}
            {connLoading ? (
              <p className="text-sm text-muted-foreground">Loading…</p>
            ) : consumers.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Not linked to any agent yet
              </p>
            ) : (
              <div className="space-y-1.5">
                {consumers.map((a) => (
                  <div
                    key={a.id}
                    className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2"
                  >
                    <AgentBadge agent={a} linkTo="agent" />
                    {(a.owner_name || a.owner_email) && (
                      <span className="text-xs text-muted-foreground truncate text-right">
                        {a.owner_name && a.owner_email
                          ? `${a.owner_name} · ${a.owner_email}`
                          : a.owner_name || a.owner_email}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
