import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import {
  AdminProviderCredentialsService,
  type AICredentialType,
  type ProviderAdminCredentialConfig,
  type ProviderAdminCredentialPublic,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import { PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY } from "@/components/Admin/LlmProviders/providerTypes"
import {
  adminConfigFields,
  useProviderAdapters,
} from "@/components/Admin/LlmProviders/useProviderAdapters"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface ProviderAdminCredentialDialogProps {
  mode: "create" | "edit"
  record?: ProviderAdminCredentialPublic
  open?: boolean
  onOpenChange?: (open: boolean) => void
}

/**
 * Connect (or re-configure) a provider organisation.
 *
 * The form is built from the adapter's own `admin_config_schema` rather than
 * from a per-provider table in here: the server is the authority on what
 * connecting an organisation requires, and it already publishes that answer.
 * The provider list is the adapters that can create keys — connecting an
 * organisation for a provider that cannot mint would buy nothing, since the
 * only thing this secret is used for is minting and revoking.
 *
 * Plain state rather than react-hook-form + zod because the config half of the
 * form is defined at runtime by the schema; a static resolver would have to
 * restate the field list the server just sent.
 */
export function ProviderAdminCredentialDialog({
  mode,
  record,
  open: controlledOpen,
  onOpenChange,
}: ProviderAdminCredentialDialogProps) {
  const [internalOpen, setInternalOpen] = useState(false)
  const isOpen = mode === "edit" ? (controlledOpen ?? false) : internalOpen
  const setIsOpen = (open: boolean) => {
    if (mode === "edit") onOpenChange?.(open)
    else setInternalOpen(open)
  }

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const {
    mintingAdapters,
    adapterFor,
    isPending: adaptersPending,
    isError: adaptersError,
  } = useProviderAdapters(isOpen)

  const [name, setName] = useState(record?.name ?? "")
  const [providerType, setProviderType] = useState<AICredentialType | "">(
    record?.provider_type ?? "",
  )
  const [secret, setSecret] = useState("")
  // Config values as strings, keyed by the schema's own field names. Coerced on
  // submit using the type each field declares.
  const [config, setConfig] = useState<Record<string, string>>(() =>
    configToStrings(record?.config),
  )
  const [errors, setErrors] = useState<Record<string, string>>({})

  const adapter = providerType ? adapterFor(providerType) : undefined
  const fields = useMemo(() => adminConfigFields(adapter), [adapter])
  // The adapter schema *is* the config half of this form, so nothing may be
  // saved until it is in hand.
  const schemaReady = !adaptersPending && !adaptersError

  // Re-seed whenever the dialog opens, so a stale edit from a closed dialog
  // never leaks back in.
  useEffect(() => {
    if (!isOpen) return
    setName(record?.name ?? "")
    setProviderType(record?.provider_type ?? "")
    setSecret("")
    setConfig(configToStrings(record?.config))
    setErrors({})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, record?.id])

  // In create mode, default to the single minting provider when there is only
  // one — the common case, and one fewer click before the real work.
  useEffect(() => {
    if (mode !== "create" || !isOpen || providerType) return
    if (mintingAdapters.length === 1) setProviderType(mintingAdapters[0].type)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, mintingAdapters.length, mode])

  const invalidate = () => {
    void queryClient.invalidateQueries({
      queryKey: PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY,
    })
  }

  const createMutation = useMutation({
    mutationFn: (body: {
      name: string
      provider_type: AICredentialType
      secret: string
      config: ProviderAdminCredentialConfig
    }) =>
      AdminProviderCredentialsService.createProviderAdminCredential({
        requestBody: body,
      }),
    onSuccess: () => {
      showSuccessToast("Provider organisation connected. Press Verify to check it.")
      setIsOpen(false)
    },
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: invalidate,
  })

  const updateMutation = useMutation({
    mutationFn: (body: {
      name?: string
      secret?: string
      config?: ProviderAdminCredentialConfig
    }) =>
      AdminProviderCredentialsService.updateProviderAdminCredential({
        credentialId: record!.id,
        requestBody: body,
      }),
    onSuccess: () => {
      showSuccessToast("Provider organisation updated.")
      setIsOpen(false)
    },
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: invalidate,
  })

  const isSaving = createMutation.isPending || updateMutation.isPending

  const submit = () => {
    // A form whose field list came from a query cannot submit before the query
    // answers. Not a validation message on a field — there is no field yet.
    if (!schemaReady) return
    const nextErrors: Record<string, string> = {}
    if (name.trim() === "") nextErrors.name = "Name is required"
    if (!providerType) nextErrors.provider_type = "Select a provider"
    if (mode === "create" && secret.trim() === "") {
      nextErrors.secret = "Admin API key is required"
    }
    for (const field of fields) {
      const value = (config[field.name] ?? "").trim()
      if (field.required && value === "") {
        nextErrors[field.name] = `${field.label} is required`
        continue
      }
      if (field.type === "integer" && value !== "" && !/^\d+$/.test(value)) {
        nextErrors[field.name] = `${field.label} must be a whole number`
      }
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return

    // Built from the schema's field names, not from a hardcoded list: the
    // adapter says which keys its configuration has, and this sends exactly
    // those. The cast is the one place that shape meets the generated type.
    const payload: Record<string, string | number | null> = {}
    for (const field of fields) {
      const value = (config[field.name] ?? "").trim()
      if (field.type === "integer") {
        payload[field.name] = value === "" ? null : Number(value)
      } else {
        payload[field.name] = value === "" ? null : value
      }
    }
    const typedConfig = payload as unknown as ProviderAdminCredentialConfig

    if (mode === "create") {
      createMutation.mutate({
        name: name.trim(),
        provider_type: providerType as AICredentialType,
        secret: secret.trim(),
        config: typedConfig,
      })
      return
    }

    // Edit: every omitted field means "leave it alone" on the server, so an
    // untouched secret is simply not sent — the surface never round-trips a
    // secret in order to rename a record.
    const body: { name?: string; secret?: string; config?: ProviderAdminCredentialConfig } =
      {}
    if (name.trim() !== (record?.name ?? "")) body.name = name.trim()
    if (secret.trim() !== "") body.secret = secret.trim()
    // Omitting `config` means "leave it alone" on the server, and that is the
    // honest request when the schema that defines it never arrived: `fields` is
    // `[]` while the adapters query is pending or has failed, so an unguarded
    // assignment sends `{}` and the admin gets a required-field 422 for a
    // rename they made without touching the configuration at all.
    if (fields.length > 0) body.config = typedConfig
    updateMutation.mutate(body)
  }

  const dialogBody = (
    <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>
          {mode === "create" ? "Connect a provider organisation" : "Edit provider key"}
        </DialogTitle>
        <DialogDescription>
          This key creates and destroys API keys for a whole provider
          organisation. It is stored encrypted, is never shown again, and is
          never handed to an agent, an environment or a desktop client.
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-4 py-2">
        <div className="space-y-2">
          <Label htmlFor="pac-name">
            Name <span className="text-destructive">*</span>
          </Label>
          <Input
            id="pac-name"
            value={name}
            placeholder="OpenAI — Engineering project"
            onChange={(event) => setName(event.target.value)}
          />
          {errors.name && (
            <p className="text-xs text-destructive">{errors.name}</p>
          )}
        </div>

        <div className="space-y-2">
          <Label>
            Provider <span className="text-destructive">*</span>
          </Label>
          <Select
            value={providerType || undefined}
            onValueChange={(value) => setProviderType(value as AICredentialType)}
            disabled={mode === "edit"}
          >
            <SelectTrigger>
              <SelectValue
                placeholder={
                  adaptersPending ? "Loading providers..." : "Select a provider"
                }
              />
            </SelectTrigger>
            <SelectContent>
              {mintingAdapters.map((entry) => (
                <SelectItem key={entry.type} value={entry.type}>
                  {entry.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">
            {mode === "edit"
              ? "The provider can't be changed after the organisation is connected."
              : "Providers whose administration API can create keys. For every other provider, a key is pasted for each person."}
          </p>
          {errors.provider_type && (
            <p className="text-xs text-destructive">{errors.provider_type}</p>
          )}
        </div>

        <div className="space-y-2">
          <Label htmlFor="pac-secret">
            Admin API key
            {mode === "create" && <span className="text-destructive"> *</span>}
          </Label>
          <Input
            id="pac-secret"
            type="password"
            autoComplete="off"
            value={secret}
            placeholder={mode === "edit" ? "Leave blank to keep the stored key" : "sk-admin-..."}
            onChange={(event) => setSecret(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {mode === "edit"
              ? "Leave blank to keep the stored key. Replacing it does not change any key already created with the old one."
              : "An administration key for the organisation, not a model key."}
          </p>
          {errors.secret && (
            <p className="text-xs text-destructive">{errors.secret}</p>
          )}
        </div>

        {fields.map((field) => (
          <div key={field.name} className="space-y-2">
            <Label htmlFor={`pac-${field.name}`}>
              {field.label}
              {field.required && <span className="text-destructive"> *</span>}
            </Label>
            <Input
              id={`pac-${field.name}`}
              inputMode={field.type === "integer" ? "numeric" : undefined}
              value={config[field.name] ?? ""}
              onChange={(event) =>
                setConfig((prev) => ({ ...prev, [field.name]: event.target.value }))
              }
            />
            {field.help && (
              <p className="text-xs text-muted-foreground">{field.help}</p>
            )}
            {errors[field.name] && (
              <p className="text-xs text-destructive">{errors[field.name]}</p>
            )}
          </div>
        ))}

        {adaptersError && (
          <p className="text-sm text-destructive">
            The provider list could not be loaded, so this form cannot be saved.
            Refresh and try again.
          </p>
        )}

        {providerType && fields.length === 0 && schemaReady && (
          <p className="text-xs text-muted-foreground">
            This provider needs no further configuration.
          </p>
        )}
      </div>

      <DialogFooter>
        <DialogClose asChild>
          <Button variant="outline" type="button" disabled={isSaving}>
            Cancel
          </Button>
        </DialogClose>
        <LoadingButton
          type="button"
          loading={isSaving}
          disabled={!schemaReady}
          onClick={submit}
        >
          {mode === "create" ? "Connect" : "Save"}
        </LoadingButton>
      </DialogFooter>
    </DialogContent>
  )

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      {mode === "create" && (
        <DialogTrigger asChild>
          <Button>
            <Plus className="mr-2 h-4 w-4" />
            Connect provider
          </Button>
        </DialogTrigger>
      )}
      {dialogBody}
    </Dialog>
  )
}

/** The stored config as editable strings, keyed as the schema names them. */
function configToStrings(
  config: ProviderAdminCredentialConfig | undefined,
): Record<string, string> {
  if (!config) return {}
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(config)) {
    out[key] = value === null || value === undefined ? "" : String(value)
  }
  return out
}
