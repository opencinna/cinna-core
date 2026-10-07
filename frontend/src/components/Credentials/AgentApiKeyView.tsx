import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { FileText } from "lucide-react"
import { useEffect } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import type { AgentApiKeyHolderInfo, CredentialWithData } from "@/client"
import { CredentialsService } from "@/client"
import { AgentApiKeyAccessCard } from "@/components/Credentials/AgentApiKeyAccessCard"
import { AgentApiKeyTopCard } from "@/components/Credentials/AgentApiKeyTopCard"
import { ServiceUriField } from "@/components/Credentials/CredentialFields/ServiceUriField"
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
import { agentApiKeyHolderQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
  service_uri: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

interface AgentApiKeyViewProps {
  credential: CredentialWithData
  /** The holder's view of the key (``GET /credentials/{id}/agent-api-key``). */
  holder: AgentApiKeyHolderInfo
  /**
   * The user just issued this key to themself (``?new=1``). Shows the value
   * once, read from the issue response the flow parked in
   * ``agentApiKeyMintHandoff`` — not from the audited reveal endpoint.
   */
  justCreated?: boolean
}

/**
 * Detail view for an issued agent-api **key**, as its holder sees it.
 *
 * One full-width card over two half-width ones, so the thing you came for is
 * not buried under configuration:
 *
 * - **{AGENT_API_KEY_LABEL}** (full width) — does it work, the value, the base
 *   URL, the curl, and Rotate (``AgentApiKeyTopCard``).
 * - **Details** (half) — name, slot and notes: how the holder and their code
 *   find the key. Naming changes nothing the key can do.
 * - **Access** (half) — what the issuer decided, read-only
 *   (``AgentApiKeyAccessCard``).
 */
export function AgentApiKeyView({
  credential,
  holder,
  justCreated,
}: AgentApiKeyViewProps) {
  return (
    <div className="space-y-6">
      <AgentApiKeyTopCard
        key={credential.id}
        credential={credential}
        holder={holder}
        justCreated={!!justCreated}
      />

      <div className="grid gap-6 lg:grid-cols-2 items-start">
        <AgentApiKeyDetailsCard credential={credential} />
        <AgentApiKeyAccessCard holder={holder} />
      </div>
    </div>
  )
}

/** Name, slot and notes — the holder's own labels for the key. */
function AgentApiKeyDetailsCard({
  credential,
}: {
  credential: CredentialWithData
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: {
      name: credential.name,
      notes: credential.notes ?? "",
      service_uri: credential.service_uri ?? "",
    },
  })

  useEffect(() => {
    form.reset({
      name: credential.name,
      notes: credential.notes ?? "",
      service_uri: credential.service_uri ?? "",
    })
  }, [credential, form])

  const metadataMutation = useMutation({
    mutationFn: (data: FormData) =>
      CredentialsService.updateCredential({
        id: credential.id,
        requestBody: data,
      }),
    // Mark the form pristine from the saved values instead of refetching the
    // whole detail page: the value is not in that query for a key anyway.
    onSuccess: (_res, variables) => {
      showSuccessToast("Key updated")
      form.reset(variables)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
      queryClient.invalidateQueries({
        queryKey: agentApiKeyHolderQueryKey(credential.id),
      })
    },
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <FileText className="h-5 w-5" />
          Details
        </CardTitle>
        <CardDescription>
          Naming is for you; the slot is how code finds the key.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit((d) => metadataMutation.mutate(d))}
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
                    <Input placeholder="Key name" type="text" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <ServiceUriField control={form.control} />
            <FormField
              control={form.control}
              name="notes"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Notes</FormLabel>
                  <FormControl>
                    <Textarea
                      placeholder="Where this key is deployed…"
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
                  disabled={metadataMutation.isPending}
                >
                  Reset
                </Button>
                <LoadingButton type="submit" loading={metadataMutation.isPending}>
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
