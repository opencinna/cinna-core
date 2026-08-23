import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { useMemo } from "react"
import { type Resolver, useForm } from "react-hook-form"
import { z } from "zod"

import {
  type ServerChannelCreate,
  type ServerChannelPublic,
  ServerChannelsService,
  type ServerChannelUpdate,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { DialogFooter } from "@/components/ui/dialog"
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
import { LoadingButton } from "@/components/ui/loading-button"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  AUTO_REGISTER_HELP,
  parseWhitelist,
  WHITELIST_EMPTY_WARNING,
  WHITELIST_HELP,
  WHITELIST_WILDCARD_WARNING,
} from "./channelCopy"
import { getChannelTypeMeta } from "./channelTypes"

interface Props {
  /** Fixed for the lifetime of this form: picked in step 1, immutable on edit. */
  channelType: string
  displayName: string
  /** null = create. */
  channel: ServerChannelPublic | null
  onCancel: () => void
  onSaved: (created: ServerChannelPublic | null) => void
}

/**
 * Declared rather than inferred from the schema: the config shape is built at
 * runtime from the type's registry entry, so `z.infer` widens `config` to an
 * index signature of `unknown` and every field loses its `string` type.
 */
interface ChannelFormValues {
  name: string
  /** Keyed by `ChannelConfigField.key`; empty for raw-JSON types. */
  config: Record<string, string>
  configJson: string
  email_whitelist: string
  auto_register_users: boolean
  enabled: boolean
  secrets: string
}

/** Non-blank string that parses to a JSON object — the raw-config escape hatch. */
function isJsonObject(value: string): boolean {
  try {
    const parsed = JSON.parse(value)
    return (
      typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
    )
  } catch {
    return false
  }
}

/**
 * Step 2 of the channel dialog: the settings for one channel type.
 *
 * The config section is driven by the type's registry entry rather than
 * hard-coded, so registering a second adapter can't leave this form demanding
 * Google Chat's project number for it. Types with no registry entry fall back
 * to a raw JSON editor.
 *
 * Mounted per type (the dialog keys it), so form state is built once at mount
 * and there is no reset effect to keep in sync.
 */
export function ServerChannelForm({
  channelType,
  displayName,
  channel,
  onCancel,
  onSaved,
}: Props) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const isEdit = channel !== null
  const meta = getChannelTypeMeta(channelType)
  const isRawConfig = meta.configFields.length === 0

  const schema = useMemo(() => {
    const configShape: Record<string, z.ZodString> = {}
    for (const field of meta.configFields) {
      let value = z.string().trim().min(1, `${field.label} is required`)
      if (field.pattern) {
        value = value.regex(field.pattern.regex, field.pattern.message)
      }
      configShape[field.key] = value
    }
    return z
      .object({
        name: z.string().min(1, "Name is required").max(255),
        config: z.object(configShape),
        configJson: z.string(),
        email_whitelist: z.string(),
        auto_register_users: z.boolean(),
        enabled: z.boolean(),
        secrets: z.string(),
      })
      .refine(
        (values) =>
          !isRawConfig ||
          !values.configJson.trim() ||
          isJsonObject(values.configJson),
        {
          path: ["configJson"],
          message: 'Must be a JSON object, e.g. { "key": "value" }',
        },
      )
  }, [meta, isRawConfig])

  const storedConfig = (channel?.config ?? {}) as Record<string, unknown>

  const form = useForm<ChannelFormValues>({
    resolver: zodResolver(schema) as unknown as Resolver<ChannelFormValues>,
    defaultValues: {
      // Pre-filled from the type so a channel is never nameless by accident;
      // an admin running two of the same type just renames it.
      name: channel?.name ?? displayName,
      config: Object.fromEntries(
        meta.configFields.map((f) => [
          f.key,
          String(storedConfig[f.key] ?? ""),
        ]),
      ),
      configJson:
        isRawConfig && Object.keys(storedConfig).length > 0
          ? JSON.stringify(storedConfig, null, 2)
          : "",
      email_whitelist: channel?.email_whitelist ?? "",
      auto_register_users: channel?.auto_register_users ?? false,
      enabled: channel?.enabled ?? true,
      // Always blank: the stored value is write-only and never sent back to
      // us, so there is nothing to prefill.
      secrets: "",
    },
  })

  const createMutation = useMutation({
    mutationFn: (body: ServerChannelCreate) =>
      ServerChannelsService.createChannel({ requestBody: body }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["serverChannels"] })
      showSuccessToast("Channel created")
      onSaved(created)
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Failed to create channel")),
  })

  const updateMutation = useMutation({
    mutationFn: (body: ServerChannelUpdate) =>
      ServerChannelsService.updateChannel({
        channelId: channel!.id,
        requestBody: body,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["serverChannels"] })
      queryClient.invalidateQueries({
        queryKey: ["serverChannelSetup", channel!.id],
      })
      showSuccessToast("Channel updated")
      onSaved(null)
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Failed to update channel")),
  })

  const onSubmit = (values: ChannelFormValues) => {
    const config: Record<string, unknown> = isRawConfig
      ? values.configJson.trim()
        ? JSON.parse(values.configJson)
        : {}
      : Object.fromEntries(
          meta.configFields.map((f) => [f.key, values.config[f.key].trim()]),
        )
    const whitelist = values.email_whitelist.trim()
    const secrets = values.secrets.trim()

    if (isEdit) {
      // `secrets` is only sent when non-empty — an untouched field must leave
      // the stored credential exactly as it was.
      const body: ServerChannelUpdate = {
        name: values.name,
        enabled: values.enabled,
        auto_register_users: values.auto_register_users,
        config,
        email_whitelist: whitelist || null,
      }
      if (secrets) body.secrets = secrets
      updateMutation.mutate(body)
      return
    }

    createMutation.mutate({
      channel_type: channelType,
      name: values.name,
      enabled: values.enabled,
      auto_register_users: values.auto_register_users,
      config,
      email_whitelist: whitelist || null,
      secrets: secrets || null,
    })
  }

  // Tokenized, not string-compared: a blanket allow can be spelled many ways
  // ("*", "*, ops@corp.com", "*@a.com, *") and every one of them must warn.
  const whitelist = parseWhitelist(form.watch("email_whitelist"))
  const isSaving = createMutation.isPending || updateMutation.isPending

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
        <FormField
          control={form.control}
          name="name"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Name</FormLabel>
              <FormControl>
                <Input placeholder={meta.namePlaceholder} {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        {isRawConfig ? (
          <FormField
            control={form.control}
            name="configJson"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Configuration (JSON)</FormLabel>
                <FormControl>
                  <Textarea
                    rows={4}
                    spellCheck={false}
                    className="font-mono text-xs"
                    placeholder="{ }"
                    {...field}
                  />
                </FormControl>
                <FormDescription>
                  This channel type has no dedicated form yet — the settings its
                  adapter expects go here as JSON.
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
        ) : (
          meta.configFields.map((cfgField) => (
            <FormField
              key={cfgField.key}
              control={form.control}
              name={`config.${cfgField.key}` as const}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{cfgField.label}</FormLabel>
                  <FormControl>
                    <Input
                      placeholder={cfgField.placeholder}
                      inputMode={cfgField.inputMode}
                      {...field}
                    />
                  </FormControl>
                  {cfgField.description && (
                    <FormDescription>{cfgField.description}</FormDescription>
                  )}
                  <FormMessage />
                </FormItem>
              )}
            />
          ))
        )}

        <FormField
          control={form.control}
          name="secrets"
          render={({ field }) => (
            <FormItem>
              <FormLabel>{meta.secrets.label}</FormLabel>
              <FormControl>
                <Textarea
                  rows={4}
                  autoComplete="off"
                  spellCheck={false}
                  className="font-mono text-xs"
                  placeholder={
                    isEdit && channel?.has_outbound_credentials
                      ? "•••• credential saved — paste a new one to replace it"
                      : meta.secrets.placeholder
                  }
                  {...field}
                />
              </FormControl>
              <FormDescription>
                {isEdit ? meta.secrets.helpEdit : meta.secrets.helpNew}
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name="email_whitelist"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Allowed senders</FormLabel>
              <FormControl>
                <Textarea
                  rows={2}
                  spellCheck={false}
                  className="font-mono text-xs"
                  placeholder="*@example.com, devops.*@support.com"
                  {...field}
                />
              </FormControl>
              <FormDescription>{WHITELIST_HELP}</FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />

        {/* Fail-closed semantics, stated rather than implied. An admin who
            reads "empty = open" into a blank box has made a security
            mistake; both non-obvious states get an explicit callout. */}
        {whitelist.isEmpty ? (
          <Alert>
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>{WHITELIST_EMPTY_WARNING}</AlertDescription>
          </Alert>
        ) : whitelist.hasWildcard ? (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>{WHITELIST_WILDCARD_WARNING}</AlertDescription>
          </Alert>
        ) : null}

        <FormField
          control={form.control}
          name="auto_register_users"
          render={({ field }) => (
            <FormItem className="flex items-start justify-between gap-4 rounded-lg border p-3">
              <div className="space-y-0.5">
                <FormLabel>Auto-register senders</FormLabel>
                <FormDescription>{AUTO_REGISTER_HELP}</FormDescription>
              </div>
              <FormControl>
                <Switch
                  checked={field.value}
                  onCheckedChange={field.onChange}
                />
              </FormControl>
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name="enabled"
          render={({ field }) => (
            <FormItem className="flex items-center justify-between gap-4 rounded-lg border p-3">
              <div className="space-y-0.5">
                <FormLabel>Enabled</FormLabel>
                <FormDescription>
                  A disabled channel stops accepting inbound messages.
                </FormDescription>
              </div>
              <FormControl>
                <Switch
                  checked={field.value}
                  onCheckedChange={field.onChange}
                />
              </FormControl>
            </FormItem>
          )}
        />

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={isSaving}>
            {isEdit ? "Save" : "Create channel"}
          </LoadingButton>
        </DialogFooter>
      </form>
    </Form>
  )
}
