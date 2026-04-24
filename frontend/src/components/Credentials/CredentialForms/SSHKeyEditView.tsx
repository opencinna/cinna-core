import { useEffect, useState } from "react"
import { zodResolver } from "@hookform/resolvers/zod"
import { useForm } from "react-hook-form"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Check, Copy, Lock, RotateCcw } from "lucide-react"
import { z } from "zod"

import {
  CredentialsService,
  type CredentialWithData,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
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
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

/**
 * Editable surface for an ssh_key credential on the credential detail page.
 *
 * Layout:
 *   Left column  — name, notes, host_aliases (editable), save button.
 *   Right column — public_key + fingerprint + key_type (read-only, with copy
 *                  buttons), rotate-key action.
 *
 * The private key is never surfaced. "Rotate key" sends the same
 * `mode=generate` payload the create flow uses; on success we surface the new
 * public key so the user can update their deploy key.
 */

interface SSHKeyEditViewProps {
  credential: CredentialWithData
}

const metadataSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
  host_aliases_text: z.string().optional(),
})

type MetadataFormData = z.infer<typeof metadataSchema>

function aliasesToText(aliases: unknown): string {
  if (!Array.isArray(aliases)) return ""
  if (aliases.length === 1 && aliases[0] === "*") return ""
  return aliases.filter((a): a is string => typeof a === "string").join(", ")
}

function parseHostAliases(text: string | undefined): string[] | undefined {
  if (!text) return undefined
  const parts = text.split(",").map((s) => s.trim()).filter(Boolean)
  return parts.length > 0 ? parts : undefined
}

export function SSHKeyEditView({ credential }: SSHKeyEditViewProps) {
  const data = credential.credential_data ?? {}
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copiedField, setCopiedField] =
    useState<null | "public_key" | "fingerprint">(null)
  const [rotateOpen, setRotateOpen] = useState(false)

  const form = useForm<MetadataFormData>({
    resolver: zodResolver(metadataSchema),
    mode: "onBlur",
    defaultValues: {
      name: credential.name,
      notes: credential.notes ?? "",
      host_aliases_text: aliasesToText(data.host_aliases),
    },
  })

  useEffect(() => {
    form.reset({
      name: credential.name,
      notes: credential.notes ?? "",
      host_aliases_text: aliasesToText(data.host_aliases),
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credential])

  const metadataMutation = useMutation({
    mutationFn: (values: MetadataFormData) => {
      const aliases = parseHostAliases(values.host_aliases_text)
      return CredentialsService.updateCredential({
        id: credential.id,
        requestBody: {
          name: values.name,
          notes: values.notes || null,
          credential_data: {
            host_aliases: aliases ?? null,
          },
        },
      })
    },
    onSuccess: () => {
      showSuccessToast("SSH key credential updated")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
      queryClient.invalidateQueries({
        queryKey: ["credential-with-data", credential.id],
      })
    },
  })

  // Rotate: generate a fresh key pair of the same type, preserving the
  // currently-persisted host_aliases. The old key files are removed on the
  // next env sync via the agent-env's orphan reconciliation.
  //
  // IMPORTANT: aliases come from `data.host_aliases` (the persisted value),
  // NOT the form input. The Rotate button is disabled while the metadata form
  // is dirty so the user must Save or Reset first — this prevents unsaved
  // metadata edits from silently riding along with a key rotation.
  const rotateMutation = useMutation({
    mutationFn: () => {
      const persistedAliases = Array.isArray(data.host_aliases)
        ? (data.host_aliases as string[])
        : undefined
      return CredentialsService.updateCredential({
        id: credential.id,
        requestBody: {
          credential_data: {
            mode: "generate",
            key_type: (data.key_type as "rsa" | "ed25519") || "ed25519",
            ...(persistedAliases ? { host_aliases: persistedAliases } : {}),
          },
        },
      })
    },
    onSuccess: () => {
      showSuccessToast(
        "New key pair generated. Update your deploy key with the new public key.",
      )
      setRotateOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
      queryClient.invalidateQueries({
        queryKey: ["credential-with-data", credential.id],
      })
    },
  })

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

  const onMetadataSubmit = (values: MetadataFormData) => {
    metadataMutation.mutate(values)
  }

  const publicKey = (data.public_key as string) || ""
  const fingerprint = (data.fingerprint as string) || ""
  const keyType = (data.key_type as string) || "—"

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      {/* Left: editable metadata */}
      <div className="space-y-4">
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit(onMetadataSubmit)}
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
                    <Input placeholder="My SSH Key" type="text" {...field} />
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

            <FormField
              control={form.control}
              name="host_aliases_text"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Host Aliases</FormLabel>
                  <FormControl>
                    <Input
                      placeholder="github.com, gitlab.com"
                      {...field}
                      value={(field.value as string) ?? ""}
                    />
                  </FormControl>
                  <p className="text-xs text-muted-foreground mt-1">
                    Comma-separated list of hosts. Leave blank to apply to all
                    SSH hosts.
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />

            {form.formState.isDirty && (
              <div className="flex justify-end gap-2 pt-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() =>
                    form.reset({
                      name: credential.name,
                      notes: credential.notes ?? "",
                      host_aliases_text: aliasesToText(data.host_aliases),
                    })
                  }
                  disabled={metadataMutation.isPending}
                >
                  Reset
                </Button>
                <LoadingButton
                  type="submit"
                  loading={metadataMutation.isPending}
                >
                  Save Changes
                </LoadingButton>
              </div>
            )}
          </form>
        </Form>
      </div>

      {/* Right: read-only key material + rotate */}
      <div className="space-y-4">
        <Alert>
          <Lock className="h-4 w-4" />
          <AlertDescription className="text-xs">
            Private key is encrypted and cannot be viewed or exported. To
            replace it, use <strong>Rotate key</strong>.
          </AlertDescription>
        </Alert>

        <div className="grid gap-2">
          <Label>Key Type</Label>
          <Input value={keyType} readOnly className="font-mono text-xs" />
        </div>

        <div className="grid gap-2">
          <div className="flex items-center justify-between">
            <Label>Public Key</Label>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => handleCopy("public_key", publicKey)}
              disabled={!publicKey}
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
            value={publicKey}
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
              onClick={() => handleCopy("fingerprint", fingerprint)}
              disabled={!fingerprint}
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
            value={fingerprint}
            readOnly
            className="font-mono text-xs"
          />
        </div>

        <div className="pt-2 space-y-2">
          <AlertDialog open={rotateOpen} onOpenChange={setRotateOpen}>
            <AlertDialogTrigger asChild>
              <Button
                type="button"
                variant="destructive"
                disabled={form.formState.isDirty}
              >
                <RotateCcw className="h-4 w-4 mr-2" />
                Rotate Key
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Rotate SSH key?</AlertDialogTitle>
                <AlertDialogDescription>
                  This will generate a new key pair and re-sync it to every
                  linked agent on their next sync. The old key will stop
                  working — you will need to update deploy keys / authorized
                  keys with the new public key.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={rotateMutation.isPending}>
                  Cancel
                </AlertDialogCancel>
                <AlertDialogAction
                  disabled={rotateMutation.isPending}
                  onClick={(e) => {
                    e.preventDefault()
                    rotateMutation.mutate()
                  }}
                >
                  {rotateMutation.isPending ? "Rotating..." : "Rotate key"}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
          {form.formState.isDirty && (
            <p className="text-xs text-muted-foreground">
              Save or reset your metadata changes before rotating the key.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
