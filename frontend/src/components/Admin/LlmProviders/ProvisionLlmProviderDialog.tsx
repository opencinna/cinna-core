import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  type AdminAICredentialCreate,
  type AICredentialType,
  AdminLlmProvidersService,
} from "@/client"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
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
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
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
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import {
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  PROVIDER_TYPE_OPTIONS,
} from "./providerTypes"

// Validation mirrors the field rules documented in ai_credentials.md:
//  - openai_compatible requires both base_url and model
//  - google may set an optional base_url
//  - all others use neither
const formSchema = z
  .object({
    name: z.string().min(1, "Name is required"),
    type: z.enum(["anthropic", "minimax", "openai", "openai_compatible", "google"]),
    api_key: z.string().min(1, "API key is required"),
    base_url: z.string().optional(),
    model: z.string().optional(),
    set_as_default: z.boolean(),
    set_user_sdk_defaults: z.boolean(),
  })
  .superRefine((data, ctx) => {
    if (data.type === "openai_compatible") {
      if (!data.base_url || data.base_url.trim() === "") {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["base_url"],
          message: "Base URL is required for OpenAI Compatible providers",
        })
      }
      if (!data.model || data.model.trim() === "") {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["model"],
          message: "Model is required for OpenAI Compatible providers",
        })
      }
    }
  })

type FormData = z.infer<typeof formSchema>

const DEFAULT_VALUES: FormData = {
  name: "",
  type: "anthropic",
  api_key: "",
  base_url: "",
  model: "",
  set_as_default: false,
  set_user_sdk_defaults: false,
}

export function ProvisionLlmProviderDialog() {
  const [isOpen, setIsOpen] = useState(false)
  const [targets, setTargets] = useState<UserAllowlistSelectedItem[]>([])
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: DEFAULT_VALUES,
  })

  const selectedType = form.watch("type") as AICredentialType
  const showBaseUrl = selectedType === "openai_compatible" || selectedType === "google"
  const showModel = selectedType === "openai_compatible"

  const resetDialog = () => {
    form.reset(DEFAULT_VALUES)
    setTargets([])
  }

  const mutation = useMutation({
    mutationFn: (body: AdminAICredentialCreate) =>
      AdminLlmProvidersService.provisionAiCredentials({ requestBody: body }),
    onSuccess: (result) => {
      const createdCount = result.created.length
      const skipped = result.skipped ?? []

      if (createdCount > 0) {
        showSuccessToast(
          `Provisioned ${createdCount} credential${createdCount !== 1 ? "s" : ""}.`,
        )
      }
      // Surface skipped targets individually so the admin knows which users
      // were not provisioned and why. Resolve user ids back to the labels the
      // admin picked so the toast is human-readable.
      if (skipped.length > 0) {
        const labelById = new Map(
          targets.map((t) => [t.userId, t.fallbackLabel || t.userId]),
        )
        showErrorToast(
          `${skipped.length} target${skipped.length !== 1 ? "s" : ""} skipped: ` +
            skipped
              .map((s) => `${labelById.get(s.user_id) ?? s.user_id} (${s.reason})`)
              .join("; "),
        )
      }
      if (createdCount === 0 && skipped.length === 0) {
        showErrorToast("No credentials were provisioned.")
      }

      resetDialog()
      setIsOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX })
    },
  })

  const onSubmit = (data: FormData) => {
    if (targets.length === 0) {
      showErrorToast("Select at least one target user.")
      return
    }

    const includesBaseUrl = data.type === "openai_compatible" || data.type === "google"
    const includesModel = data.type === "openai_compatible"

    const body: AdminAICredentialCreate = {
      name: data.name.trim(),
      type: data.type,
      api_key: data.api_key,
      base_url: includesBaseUrl ? data.base_url?.trim() || undefined : undefined,
      model: includesModel ? data.model?.trim() || undefined : undefined,
      target_user_ids: targets.map((t) => t.userId),
      set_as_default: data.set_as_default,
      set_user_sdk_defaults: data.set_user_sdk_defaults,
    }
    mutation.mutate(body)
  }

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        setIsOpen(open)
        if (!open) resetDialog()
      }}
    >
      <DialogTrigger asChild>
        <Button>
          <Plus className="mr-2 h-4 w-4" />
          Provision Credential
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Provision LLM Provider Credential</DialogTitle>
          <DialogDescription>
            Create a read-only AI credential on behalf of one or more users. Each
            user receives an independent credential they can use and set as their
            default, but cannot edit or delete.
          </DialogDescription>
        </DialogHeader>
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
                    <Input placeholder="Company Anthropic Key" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="type"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    Provider Type <span className="text-destructive">*</span>
                  </FormLabel>
                  <Select onValueChange={field.onChange} value={field.value}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Select a provider" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {PROVIDER_TYPE_OPTIONS.map((opt) => (
                        <SelectItem key={opt.value} value={opt.value}>
                          {opt.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormDescription>
                    {PROVIDER_TYPE_OPTIONS.find((o) => o.value === selectedType)?.description}
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="api_key"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    API Key <span className="text-destructive">*</span>
                  </FormLabel>
                  <FormControl>
                    <Input type="password" placeholder="sk-..." autoComplete="off" {...field} />
                  </FormControl>
                  <FormDescription>
                    Shared into each target user's independent credential row at rest.
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            {showBaseUrl && (
              <FormField
                control={form.control}
                name="base_url"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      Base URL
                      {selectedType === "openai_compatible" && (
                        <span className="text-destructive"> *</span>
                      )}
                    </FormLabel>
                    <FormControl>
                      <Input
                        placeholder="https://api.example.com/v1"
                        {...field}
                        value={field.value ?? ""}
                      />
                    </FormControl>
                    <FormDescription>
                      {selectedType === "google"
                        ? "Optional endpoint override for Google AI."
                        : "Endpoint for the OpenAI-compatible API."}
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            )}

            {showModel && (
              <FormField
                control={form.control}
                name="model"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      Model <span className="text-destructive">*</span>
                    </FormLabel>
                    <FormControl>
                      <Input
                        placeholder="meta-llama/Llama-3-70b"
                        {...field}
                        value={field.value ?? ""}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            )}

            <div className="space-y-2">
              <Label className="text-sm font-medium">
                Target Users <span className="text-destructive">*</span>
              </Label>
              <UserAllowlistPicker
                label={null}
                enabled={isOpen}
                selected={targets}
                searchPlaceholder="Search users to provision for..."
                emptyHint="Select one or more users to provision this credential for."
                onAdd={(user) =>
                  setTargets((prev) =>
                    prev.some((t) => t.userId === user.id)
                      ? prev
                      : [
                          ...prev,
                          {
                            id: user.id,
                            userId: user.id,
                            fallbackLabel: user.full_name || user.email,
                          },
                        ],
                  )
                }
                onRemove={(item) =>
                  setTargets((prev) => prev.filter((t) => t.userId !== item.userId))
                }
              />
            </div>

            <FormField
              control={form.control}
              name="set_as_default"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between rounded-md border p-3">
                  <div className="space-y-0.5 pr-4">
                    <FormLabel>Set as default</FormLabel>
                    <FormDescription>
                      Make this each user's default credential for its provider type.
                    </FormDescription>
                  </div>
                  <FormControl>
                    <Switch checked={field.value} onCheckedChange={field.onChange} />
                  </FormControl>
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="set_user_sdk_defaults"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between rounded-md border p-3">
                  <div className="space-y-0.5 pr-4">
                    <FormLabel>Set user SDK defaults</FormLabel>
                    <FormDescription>
                      Wire each user's conversation and building SDK defaults to this
                      credential.
                    </FormDescription>
                  </div>
                  <FormControl>
                    <Switch checked={field.value} onCheckedChange={field.onChange} />
                  </FormControl>
                </FormItem>
              )}
            />

            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline" type="button" disabled={mutation.isPending}>
                  Cancel
                </Button>
              </DialogClose>
              <LoadingButton type="submit" loading={mutation.isPending}>
                Provision
              </LoadingButton>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
