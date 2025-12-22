import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { CredentialsService } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
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
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import DeleteCredential from "@/components/Credentials/DeleteCredential"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
  credential_data: z.record(z.any()).optional(),
})

type FormData = z.infer<typeof formSchema>

function getCredentialTypeLabel(type: string): string {
  switch (type) {
    case "email_imap":
      return "Email (IMAP)"
    case "odoo":
      return "Odoo"
    case "gmail_oauth":
      return "Gmail OAuth"
    default:
      return type
  }
}

export const Route = createFileRoute("/_layout/credential/$credentialId")({
  component: CredentialDetail,
})

function CredentialDetail() {
  const { credentialId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)

  const { data: credentialWithData, isLoading, error } = useQuery({
    queryKey: ["credential", credentialId],
    queryFn: () => CredentialsService.readCredentialWithData({ id: credentialId }),
    enabled: !!credentialId,
  })

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      name: "",
      notes: "",
      credential_data: {},
    },
  })

  useEffect(() => {
    if (credentialWithData) {
      form.reset({
        name: credentialWithData.name,
        notes: credentialWithData.notes ?? undefined,
        credential_data: credentialWithData.credential_data ?? {},
      })
    }
  }, [credentialWithData, form])

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      CredentialsService.updateCredential({ id: credentialId, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Credential updated successfully")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credentialId] })
    },
  })

  const onSubmit = (data: FormData) => {
    mutation.mutate(data)
  }

  const handleDeleteSuccess = () => {
    navigate({ to: "/credentials" })
  }

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !credentialWithData) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading credential details</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => navigate({ to: "/credentials" })}
          >
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">
              {credentialWithData.name}
            </h1>
            <p className="text-muted-foreground">
              {getCredentialTypeLabel(credentialWithData.type)}
            </p>
          </div>
        </div>
        <DeleteCredential
          credential={credentialWithData}
          onSuccess={handleDeleteSuccess}
          isOpen={isDeleteOpen}
          setIsOpen={setIsDeleteOpen}
        >
          <Button variant="destructive" size="sm">
            <Trash2 className="mr-2 h-4 w-4" />
            Delete
          </Button>
        </DeleteCredential>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Credential Details</CardTitle>
          <CardDescription>
            Update your credential information below.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              <FormField
                control={form.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      Name <span className="text-destructive">*</span>
                    </FormLabel>
                    <FormControl>
                      <Input placeholder="My Credential" type="text" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              {credentialWithData.type === "email_imap" && (
                <>
                  <FormField
                    control={form.control}
                    name="credential_data.host"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Host <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input placeholder="imap.gmail.com" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.port"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Port <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input type="number" placeholder="993" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.login"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Login <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input placeholder="user@example.com" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.password"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Password <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input type="password" placeholder="••••••••" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.is_ssl"
                    render={({ field }) => (
                      <FormItem className="flex flex-row items-start space-x-3 space-y-0">
                        <FormControl>
                          <Checkbox
                            checked={field.value as boolean}
                            onCheckedChange={field.onChange}
                          />
                        </FormControl>
                        <div className="space-y-1 leading-none">
                          <FormLabel>Use SSL</FormLabel>
                        </div>
                      </FormItem>
                    )}
                  />
                </>
              )}

              {credentialWithData.type === "odoo" && (
                <>
                  <FormField
                    control={form.control}
                    name="credential_data.url"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          URL <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input placeholder="https://your-odoo.com" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.database_name"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Database Name <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input placeholder="production" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.login"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Login <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input placeholder="admin" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.api_token"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          API Token <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Input type="password" placeholder="••••••••" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                </>
              )}

              {credentialWithData.type === "gmail_oauth" && (
                <>
                  <FormField
                    control={form.control}
                    name="credential_data.access_token"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          Access Token <span className="text-destructive">*</span>
                        </FormLabel>
                        <FormControl>
                          <Textarea placeholder="ya29.a0..." {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.refresh_token"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Refresh Token</FormLabel>
                        <FormControl>
                          <Textarea placeholder="1//0g..." {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.token_type"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Token Type</FormLabel>
                        <FormControl>
                          <Input placeholder="Bearer" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name="credential_data.scope"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Scope</FormLabel>
                        <FormControl>
                          <Input
                            placeholder="https://www.googleapis.com/auth/gmail.readonly"
                            {...field}
                          />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                </>
              )}

              <FormField
                control={form.control}
                name="notes"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Notes</FormLabel>
                    <FormControl>
                      <Textarea placeholder="Additional notes..." {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => navigate({ to: "/credentials" })}
                  disabled={mutation.isPending}
                >
                  Cancel
                </Button>
                <LoadingButton type="submit" loading={mutation.isPending}>
                  Save Changes
                </LoadingButton>
              </div>
            </form>
          </Form>
        </CardContent>
      </Card>
    </div>
  )
}
