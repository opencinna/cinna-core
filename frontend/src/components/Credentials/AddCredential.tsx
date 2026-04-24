import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { Check, Copy, Plus } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  CredentialsService,
  type CredentialCreate,
  type CredentialWithData,
} from "@/client"
import { SSHKeyFields } from "@/components/Credentials/CredentialFields"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { handleError } from "@/utils"

const CREDENTIAL_TYPES = [
  "email_imap",
  "email_smtp",
  "odoo",
  "gmail_oauth",
  "gmail_oauth_readonly",
  "gdrive_oauth",
  "gdrive_oauth_readonly",
  "gcalendar_oauth",
  "gcalendar_oauth_readonly",
  "google_service_account",
  "api_token",
  "ssh_key",
] as const

const basicFormSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  type: z.enum(CREDENTIAL_TYPES),
})

type BasicFormData = z.infer<typeof basicFormSchema>

/**
 * Shape passed to SSHKeyFields via react-hook-form. `host_aliases_text` is a
 * free-text comma-separated input; we normalise it into an array immediately
 * before the POST request.
 */
type SSHKeyFormData = {
  name: string
  notes?: string
  credential_data: {
    mode: "generate" | "import"
    key_type?: "rsa" | "ed25519"
    public_key?: string
    private_key?: string
    host_aliases_text?: string
  }
}

const sshKeyFormSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
  credential_data: z
    .object({
      mode: z.enum(["generate", "import"]),
      key_type: z.enum(["rsa", "ed25519"]).optional(),
      public_key: z.string().optional(),
      private_key: z.string().optional(),
      host_aliases_text: z.string().optional(),
    })
    .superRefine((data, ctx) => {
      if (data.mode === "import") {
        if (!data.public_key?.trim()) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ["public_key"],
            message: "Public key is required for import mode",
          })
        }
        if (!data.private_key?.trim()) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ["private_key"],
            message: "Private key is required for import mode",
          })
        }
      }
    }),
})

/**
 * Parse a comma-separated string into a list of host aliases.
 * Returns `undefined` when the user left it blank (so the backend falls back
 * to its default of `["*"]`).
 */
function parseHostAliases(text: string | undefined): string[] | undefined {
  if (!text) return undefined
  const parts = text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
  return parts.length > 0 ? parts : undefined
}

const AddCredential = () => {
  const [isOpen, setIsOpen] = useState(false)
  // Two-step flow: pick name+type first (basic step), then (for ssh_key) fill
  // in key-specific fields; for other types we keep the legacy behaviour of
  // creating a skeleton record and navigating to the detail page.
  const [step, setStep] = useState<"basic" | "ssh_key" | "ssh_key_success">(
    "basic",
  )
  const [createdSshKey, setCreatedSshKey] = useState<CredentialWithData | null>(
    null,
  )
  const [copiedField, setCopiedField] =
    useState<null | "public_key" | "fingerprint">(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { activeWorkspaceId } = useWorkspace()

  const basicForm = useForm<BasicFormData>({
    resolver: zodResolver(basicFormSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      name: "",
      type: "email_imap",
    },
  })

  const sshKeyForm = useForm<SSHKeyFormData>({
    resolver: zodResolver(sshKeyFormSchema) as any,
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      name: "",
      notes: "",
      credential_data: {
        mode: "generate",
        key_type: "ed25519",
        public_key: "",
        private_key: "",
        host_aliases_text: "",
      },
    },
  })

  const resetAll = () => {
    basicForm.reset()
    sshKeyForm.reset()
    setStep("basic")
    setCreatedSshKey(null)
    setCopiedField(null)
  }

  // Mutation for non-ssh_key types: legacy behaviour — create a skeleton
  // credential and navigate to the detail page where the user fills in data.
  const basicMutation = useMutation({
    mutationFn: (data: CredentialCreate) =>
      CredentialsService.createCredential({ requestBody: data }),
    onSuccess: (credential) => {
      showSuccessToast("Credential created successfully")
      resetAll()
      setIsOpen(false)
      navigate({
        to: "/credential/$credentialId",
        params: { credentialId: credential.id },
      })
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
    },
  })

  // Mutation for ssh_key: backend requires full credential_data at create time
  // (mode + key material). On success we fetch-with-data so we can render the
  // public key + fingerprint in an inline success view before closing.
  const sshKeyMutation = useMutation({
    mutationFn: async (payload: CredentialCreate) => {
      const created = await CredentialsService.createCredential({
        requestBody: payload,
      })
      const full = await CredentialsService.readCredentialWithData({
        id: created.id,
      })
      return full
    },
    onSuccess: (full) => {
      // Seed the TanStack Query cache so the detail page (reached via "Done")
      // renders instantly with the already-fetched data instead of re-fetching
      // and flickering its public-key / fingerprint panel.
      queryClient.setQueryData(["credential-with-data", full.id], full)
      setCreatedSshKey(full)
      setStep("ssh_key_success")
      showSuccessToast("SSH key credential created")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
    },
  })

  const onBasicSubmit = (data: BasicFormData) => {
    // Intercept ssh_key — switch to the ssh_key form step instead of creating
    // a skeleton record (the backend requires credential_data at creation).
    if (data.type === "ssh_key") {
      sshKeyForm.setValue("name", data.name)
      setStep("ssh_key")
      return
    }

    basicMutation.mutate({
      ...data,
      user_workspace_id: activeWorkspaceId || undefined,
    })
  }

  const onSshKeySubmit = (data: SSHKeyFormData) => {
    const {
      mode,
      key_type,
      public_key,
      private_key,
      host_aliases_text,
    } = data.credential_data

    const credential_data: Record<string, unknown> = { mode }
    const aliases = parseHostAliases(host_aliases_text)
    if (aliases) {
      credential_data.host_aliases = aliases
    }
    if (mode === "generate") {
      credential_data.key_type = key_type || "ed25519"
    } else {
      credential_data.public_key = (public_key || "").trim()
      credential_data.private_key = (private_key || "").trim()
    }

    sshKeyMutation.mutate({
      name: data.name,
      type: "ssh_key",
      notes: data.notes || undefined,
      credential_data,
      user_workspace_id: activeWorkspaceId || undefined,
    } as CredentialCreate)
  }

  const handleCopy = async (
    field: "public_key" | "fingerprint",
    value: string,
  ) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopiedField(field)
      setTimeout(() => setCopiedField(null), 2000)
    } catch {
      showErrorToast("Failed to copy to clipboard")
    }
  }

  const handleOpenChange = (open: boolean) => {
    setIsOpen(open)
    if (!open) {
      resetAll()
    }
  }

  const handleFinishSshSuccess = () => {
    if (createdSshKey) {
      const credentialId = createdSshKey.id
      resetAll()
      setIsOpen(false)
      navigate({
        to: "/credential/$credentialId",
        params: { credentialId },
      })
    } else {
      resetAll()
      setIsOpen(false)
    }
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button className="my-4">
          <Plus className="mr-2" />
          Add Credential
        </Button>
      </DialogTrigger>

      {/* Step 1: name + type picker */}
      {step === "basic" && (
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Add Credential</DialogTitle>
            <DialogDescription>
              Provide a name and select the type. You'll configure the details
              on the next page.
            </DialogDescription>
          </DialogHeader>
          <Form {...basicForm}>
            <form onSubmit={basicForm.handleSubmit(onBasicSubmit)}>
              <div className="grid gap-4 py-4">
                <FormField
                  control={basicForm.control}
                  name="name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>
                        Name <span className="text-destructive">*</span>
                      </FormLabel>
                      <FormControl>
                        <Input
                          placeholder="My Credential"
                          type="text"
                          {...field}
                          required
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={basicForm.control}
                  name="type"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>
                        Type <span className="text-destructive">*</span>
                      </FormLabel>
                      <Select
                        onValueChange={field.onChange}
                        defaultValue={field.value}
                      >
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue placeholder="Select credential type" />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          <SelectItem value="email_imap">Email (IMAP)</SelectItem>
                          <SelectItem value="email_smtp">Email (SMTP)</SelectItem>
                          <SelectItem value="odoo">Odoo</SelectItem>
                          <SelectItem value="gmail_oauth">Gmail OAuth</SelectItem>
                          <SelectItem value="gmail_oauth_readonly">
                            Gmail OAuth (Read-Only)
                          </SelectItem>
                          <SelectItem value="gdrive_oauth">
                            Google Drive OAuth
                          </SelectItem>
                          <SelectItem value="gdrive_oauth_readonly">
                            Google Drive OAuth (Read-Only)
                          </SelectItem>
                          <SelectItem value="gcalendar_oauth">
                            Google Calendar OAuth
                          </SelectItem>
                          <SelectItem value="gcalendar_oauth_readonly">
                            Google Calendar OAuth (Read-Only)
                          </SelectItem>
                          <SelectItem value="google_service_account">
                            Google Service Account
                          </SelectItem>
                          <SelectItem value="api_token">API Token</SelectItem>
                          <SelectItem value="ssh_key">SSH Key</SelectItem>
                        </SelectContent>
                      </Select>
                      {basicForm.watch("type") === "ssh_key" && (
                        <p className="text-xs text-muted-foreground mt-1">
                          Private key for{" "}
                          <code>git clone git@…</code> / SSH access from inside
                          agent containers.
                        </p>
                      )}
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </div>

              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="outline" disabled={basicMutation.isPending}>
                    Cancel
                  </Button>
                </DialogClose>
                <LoadingButton type="submit" loading={basicMutation.isPending}>
                  {basicForm.watch("type") === "ssh_key" ? "Next" : "Save"}
                </LoadingButton>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      )}

      {/* Step 2 (ssh_key only): full key form */}
      {step === "ssh_key" && (
        <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>New SSH Key Credential</DialogTitle>
            <DialogDescription>
              Generate a new key pair or paste an existing one. The public key
              will be shown on the next screen so you can register it as a
              deploy key / authorized key.
            </DialogDescription>
          </DialogHeader>
          <Form {...sshKeyForm}>
            <form onSubmit={sshKeyForm.handleSubmit(onSshKeySubmit)}>
              <div className="py-4">
                <SSHKeyFields
                  control={sshKeyForm.control}
                  watch={sshKeyForm.watch}
                />
              </div>
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setStep("basic")}
                  disabled={sshKeyMutation.isPending}
                >
                  Back
                </Button>
                <LoadingButton
                  type="submit"
                  loading={sshKeyMutation.isPending}
                >
                  Create
                </LoadingButton>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      )}

      {/* Step 3 (ssh_key only): success view with public key + fingerprint */}
      {step === "ssh_key_success" && createdSshKey && (
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>SSH Key Credential Created</DialogTitle>
            <DialogDescription>
              Copy the public key below and add it as a deploy key on GitHub
              (Settings → Deploy keys) or as an authorized key on your target
              host. The private key has been encrypted and will never be shown
              again.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <div className="flex items-center justify-between">
                <Label>Public Key</Label>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    handleCopy(
                      "public_key",
                      (createdSshKey.credential_data?.public_key as string) ||
                        "",
                    )
                  }
                >
                  {copiedField === "public_key" ? (
                    <>
                      <Check className="h-4 w-4 mr-2" />
                      Copied!
                    </>
                  ) : (
                    <>
                      <Copy className="h-4 w-4 mr-2" />
                      Copy
                    </>
                  )}
                </Button>
              </div>
              <Textarea
                value={
                  (createdSshKey.credential_data?.public_key as string) || ""
                }
                readOnly
                className="font-mono text-xs h-32"
              />
            </div>
            <div className="grid gap-2">
              <div className="flex items-center justify-between">
                <Label>Fingerprint</Label>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    handleCopy(
                      "fingerprint",
                      (createdSshKey.credential_data?.fingerprint as string) ||
                        "",
                    )
                  }
                >
                  {copiedField === "fingerprint" ? (
                    <>
                      <Check className="h-4 w-4 mr-2" />
                      Copied!
                    </>
                  ) : (
                    <>
                      <Copy className="h-4 w-4 mr-2" />
                      Copy
                    </>
                  )}
                </Button>
              </div>
              <Input
                value={
                  (createdSshKey.credential_data?.fingerprint as string) || ""
                }
                readOnly
                className="font-mono text-xs"
              />
            </div>
          </div>
          <DialogFooter>
            <Button onClick={handleFinishSshSuccess}>Done</Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  )
}

export default AddCredential
