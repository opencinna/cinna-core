import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Pencil } from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { type CredentialPublic, CredentialsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
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
import {
  EmailImapFields,
  OdooFields,
  GmailOAuthFields,
  ApiTokenFields,
} from "@/components/Credentials/CredentialFields"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
  credential_data: z.record(z.any()).optional(),
})

type FormData = z.infer<typeof formSchema>

interface EditCredentialProps {
  credential: CredentialPublic
  onSuccess: () => void
}

const EditCredential = ({ credential, onSuccess }: EditCredentialProps) => {
  const [isOpen, setIsOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: credentialWithData, isLoading } = useQuery({
    queryKey: ["credential", credential.id],
    queryFn: () => CredentialsService.readCredentialWithData({ id: credential.id }),
    enabled: isOpen,
  })

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      name: credential.name,
      notes: credential.notes ?? undefined,
      credential_data: {},
    },
  })

  useEffect(() => {
    if (credentialWithData) {
      form.reset({
        name: credentialWithData.name,
        notes: credentialWithData.notes ?? undefined,
        credential_data: credentialWithData.credential_data,
      })
    }
  }, [credentialWithData, form])

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      CredentialsService.updateCredential({ id: credential.id, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Credential updated successfully")
      setIsOpen(false)
      onSuccess()
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
    },
  })

  const onSubmit = (data: FormData) => {
    mutation.mutate(data)
  }

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuItem
        onSelect={(e) => e.preventDefault()}
        onClick={() => setIsOpen(true)}
      >
        <Pencil />
        Edit Credential
      </DropdownMenuItem>
      <DialogContent className="sm:max-w-2xl max-h-[90vh] overflow-y-auto">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <div className="text-muted-foreground">Loading...</div>
          </div>
        ) : (
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)}>
              <DialogHeader>
                <DialogTitle>Edit Credential</DialogTitle>
                <DialogDescription>
                  Update the credential details below.
                </DialogDescription>
              </DialogHeader>
              <div className="grid gap-4 py-4">
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

                {credential.type === "email_imap" && (
                  <EmailImapFields control={form.control} />
                )}

                {credential.type === "odoo" && (
                  <OdooFields control={form.control} />
                )}

                {credential.type === "gmail_oauth" && (
                  <GmailOAuthFields control={form.control} />
                )}

                {credential.type === "api_token" && (
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
              </div>

              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="outline" disabled={mutation.isPending}>
                    Cancel
                  </Button>
                </DialogClose>
                <LoadingButton type="submit" loading={mutation.isPending}>
                  Save
                </LoadingButton>
              </DialogFooter>
            </form>
          </Form>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default EditCredential
