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
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import DeleteCredential from "@/components/Credentials/DeleteCredential"
import { usePageHeader } from "@/routes/_layout"
import {
  EmailImapFields,
  OdooFields,
  GmailOAuthFields,
  ApiTokenFields,
} from "@/components/Credentials/CredentialFields"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
  credential_data: z.object({}).passthrough().optional(),
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
    case "api_token":
      return "API Token"
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
  const { setHeaderContent } = usePageHeader()
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

  const handleBack = () => {
    navigate({ to: "/credentials" })
  }

  // Update header when credential loads
  useEffect(() => {
    if (credentialWithData) {
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <h1 className="text-base font-semibold truncate">
                {credentialWithData.name}
              </h1>
              <p className="text-xs text-muted-foreground">
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
            <Button variant="destructive" size="sm" className="shrink-0">
              <Trash2 className="mr-2 h-4 w-4" />
              Delete
            </Button>
          </DeleteCredential>
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [credentialWithData, setHeaderContent, isDeleteOpen])

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
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
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
                <EmailImapFields control={form.control} />
              )}

              {credentialWithData.type === "odoo" && (
                <OdooFields control={form.control} />
              )}

              {credentialWithData.type === "gmail_oauth" && (
                <GmailOAuthFields control={form.control} />
              )}

              {credentialWithData.type === "api_token" && (
                <ApiTokenFields control={form.control} watch={form.watch} />
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
    </div>
  )
}
