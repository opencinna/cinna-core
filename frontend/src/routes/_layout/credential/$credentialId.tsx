import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft, Trash2, Users, Lock } from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { CredentialsService } from "@/client"
import type { CredentialPublic, CredentialWithData } from "@/client"
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
import { Badge } from "@/components/ui/badge"
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import DeleteCredential from "@/components/Credentials/DeleteCredential"
import { usePageHeader } from "@/routes/_layout"
import { OAuthCredentialFields } from "@/components/Credentials/CredentialFields"
import {
  OdooCredentialForm,
  ApiTokenCredentialForm,
  OAuthCredentialForm,
  GenericCredentialForm,
  ServiceAccountCredentialForm,
} from "@/components/Credentials/CredentialForms"
import { CredentialSharing } from "@/components/Credentials/CredentialSharing"

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
    case "gmail_oauth_readonly":
      return "Gmail OAuth (Read-Only)"
    case "gdrive_oauth":
      return "Google Drive OAuth"
    case "gdrive_oauth_readonly":
      return "Google Drive OAuth (Read-Only)"
    case "gcalendar_oauth":
      return "Google Calendar OAuth"
    case "gcalendar_oauth_readonly":
      return "Google Calendar OAuth (Read-Only)"
    case "google_service_account":
      return "Google Service Account"
    case "api_token":
      return "API Token"
    default:
      return type
  }
}

export const Route = createFileRoute("/_layout/credential/$credentialId")({
  component: CredentialDetail,
})

// Read-only view for shared credentials
function SharedCredentialView({ credential }: { credential: CredentialPublic }) {
  return (
    <div className="space-y-6">
      <Alert>
        <Users className="h-4 w-4" />
        <AlertTitle>Shared Credential</AlertTitle>
        <AlertDescription>
          This credential was shared with you by {credential.owner_email}.
          You can use it in your agents but cannot view or edit the credential details.
        </AlertDescription>
      </Alert>

      <Card>
        <CardHeader>
          <CardTitle>Credential Information</CardTitle>
          <CardDescription>
            Basic information about this shared credential.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <label className="text-sm font-medium text-muted-foreground">Name</label>
            <p className="text-base">{credential.name}</p>
          </div>

          <div>
            <label className="text-sm font-medium text-muted-foreground">Type</label>
            <p className="text-base">{getCredentialTypeLabel(credential.type)}</p>
          </div>

          {credential.notes && (
            <div>
              <label className="text-sm font-medium text-muted-foreground">Notes</label>
              <p className="text-base">{credential.notes}</p>
            </div>
          )}

          <div>
            <label className="text-sm font-medium text-muted-foreground">Shared by</label>
            <p className="text-base">{credential.owner_email}</p>
          </div>

          <div className="flex items-center gap-2 pt-2">
            <Badge variant="outline" className="gap-1 bg-blue-50 text-blue-700 border-blue-200">
              <Users className="h-3 w-3" />
              Shared with you
            </Badge>
            <Badge variant="outline" className="gap-1">
              <Lock className="h-3 w-3" />
              Read-only
            </Badge>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

// Full edit view for owned credentials
function OwnedCredentialView({ credential }: { credential: CredentialWithData }) {
  const { credentialId } = Route.useParams()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      name: credential.name,
      notes: credential.notes ?? "",
      credential_data: credential.credential_data ?? {},
    },
  })

  useEffect(() => {
    form.reset({
      name: credential.name,
      notes: credential.notes ?? "",
      credential_data: credential.credential_data ?? {},
    })
  }, [credential, form])

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
      queryClient.invalidateQueries({ queryKey: ["credential-with-data", credentialId] })
    },
  })

  const onSubmit = (data: FormData) => {
    mutation.mutate(data)
  }

  const isOAuthCredential = [
    "gmail_oauth",
    "gmail_oauth_readonly",
    "gdrive_oauth",
    "gdrive_oauth_readonly",
    "gcalendar_oauth",
    "gcalendar_oauth_readonly",
  ].includes(credential.type)

  return (
    <div className="space-y-6">
      {/* Two-column layout for OAuth credentials */}
      {isOAuthCredential ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Left Card: Basic Information */}
          <Card>
            <CardHeader>
              <CardTitle>Basic Information</CardTitle>
              <CardDescription>
                Update the name and notes for this credential.
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
                    <div className="flex justify-end gap-2 pt-2">
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
            </CardContent>
          </Card>

          {/* Right Card: OAuth Authorization */}
          <OAuthCredentialFields
            credentialType={credential.type}
            credentialId={credential.id}
          />
        </div>
      ) : (
        /* Single card layout for non-OAuth credentials */
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
                {credential.type === "odoo" && (
                  <OdooCredentialForm form={form} />
                )}

                {credential.type === "api_token" && (
                  <ApiTokenCredentialForm form={form} />
                )}

                {(credential.type === "gmail_oauth" ||
                  credential.type === "gmail_oauth_readonly" ||
                  credential.type === "gdrive_oauth" ||
                  credential.type === "gdrive_oauth_readonly" ||
                  credential.type === "gcalendar_oauth" ||
                  credential.type === "gcalendar_oauth_readonly") && (
                  <OAuthCredentialForm
                    form={form}
                    credentialType={credential.type}
                    credentialId={credential.id}
                  />
                )}

                {credential.type === "email_imap" && (
                  <GenericCredentialForm
                    form={form}
                    credentialType={credential.type}
                  />
                )}

                {credential.type === "google_service_account" && (
                  <ServiceAccountCredentialForm form={form} />
                )}

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
          </CardContent>
        </Card>
      )}

      {/* Sharing Section - Only for owned credentials */}
      <CredentialSharing credential={credential} />
    </div>
  )
}

function CredentialDetail() {
  const { credentialId } = Route.useParams()
  const navigate = useNavigate()
  const { setHeaderContent } = usePageHeader()
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)

  // First, fetch credential metadata to check if it's shared
  const { data: credentialMeta, isLoading: metaLoading, error: metaError } = useQuery({
    queryKey: ["credential", credentialId],
    queryFn: () => CredentialsService.readCredential({ id: credentialId }),
    enabled: !!credentialId,
  })

  // If owned, fetch with data for editing
  const { data: credentialWithData, isLoading: dataLoading } = useQuery({
    queryKey: ["credential-with-data", credentialId],
    queryFn: () => CredentialsService.readCredentialWithData({ id: credentialId }),
    enabled: !!credentialId && credentialMeta?.is_shared === false,
  })

  const handleDeleteSuccess = () => {
    navigate({ to: "/credentials" })
  }

  const handleBack = () => {
    navigate({ to: "/credentials" })
  }

  // Update header when credential loads
  useEffect(() => {
    if (credentialMeta) {
      const isShared = credentialMeta.is_shared === true
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h1 className="text-base font-semibold truncate">
                  {credentialMeta.name}
                </h1>
                {isShared && (
                  <Badge variant="outline" className="gap-1 bg-blue-50 text-blue-700 border-blue-200 text-xs">
                    <Users className="h-3 w-3" />
                    Shared
                  </Badge>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                {getCredentialTypeLabel(credentialMeta.type)}
                {isShared && credentialMeta.owner_email && ` - Shared by ${credentialMeta.owner_email}`}
              </p>
            </div>
          </div>
          {/* Only show delete button for owned credentials */}
          {!isShared && credentialMeta && (
            <DeleteCredential
              credential={credentialMeta}
              onSuccess={handleDeleteSuccess}
              isOpen={isDeleteOpen}
              setIsOpen={setIsDeleteOpen}
            >
              <Button variant="destructive" size="sm" className="shrink-0">
                <Trash2 className="mr-2 h-4 w-4" />
                Delete
              </Button>
            </DeleteCredential>
          )}
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [credentialMeta, setHeaderContent, isDeleteOpen])

  if (metaLoading) {
    return <PendingItems />
  }

  if (metaError || !credentialMeta) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading credential details</p>
      </div>
    )
  }

  const isShared = credentialMeta.is_shared === true

  // For shared credentials, show read-only view immediately
  if (isShared) {
    return (
      <div className="p-6 md:p-8 overflow-y-auto">
        <div className="mx-auto max-w-7xl">
          <SharedCredentialView credential={credentialMeta} />
        </div>
      </div>
    )
  }

  // For owned credentials, wait for full data to load
  if (dataLoading || !credentialWithData) {
    return <PendingItems />
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <OwnedCredentialView credential={credentialWithData} />
      </div>
    </div>
  )
}
