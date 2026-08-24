import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { useId, useMemo, useState } from "react"
import { type Resolver, useForm } from "react-hook-form"
import { z } from "zod"

import {
  type ServerChannelCreate,
  type ServerChannelPublic,
  ServerChannelsService,
  type ServerChannelUpdate,
} from "@/client"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
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
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { getErrorMessage } from "@/utils"
import {
  AGENT_SCOPE_OPTIONS,
  ALLOW_AUTO_INSTALL_HELP,
  AUTO_REGISTER_HELP,
  asAgentScope,
  asVisibility,
  DEFAULT_AGENT_SCOPE_HELP,
  DEFAULT_ENABLED_HELP,
  NEW_CHANNEL_AGENT_SCOPE,
  NEW_CHANNEL_VISIBILITY,
  NO_GRANTS_WARNING,
  parseWhitelist,
  VISIBILITY_HELP,
  VISIBILITY_OPTIONS,
  VISIBILITY_RESTRICTED,
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
  /** `"public"` | `"restricted"` — who may use the channel at all. */
  visibility: string
  /** The four fields below are *defaults* inherited by users with no settings. */
  default_enabled_for_users: boolean
  /** `"all"` | `"list"` | `"none"`. */
  default_agent_scope: string
  allow_auto_install: boolean
}

/**
 * A radio group that looks like a segmented control.
 *
 * Two or three mutually exclusive values with a sentence of explanation each —
 * a `Select` would hide the explanations behind a click, and the whole point of
 * these controls is that the admin reads what they are choosing between. Laid
 * out as a strip rather than as `ui/radio-group`'s vertical list because the
 * selected value has to be legible against its alternatives at a glance.
 */
function SegmentedField({
  value,
  onChange,
  options,
  legend,
}: {
  value: string
  onChange: (next: string) => void
  options: ReadonlyArray<{
    value: string
    label: string
    description: string
  }>
  /**
   * The group's accessible name.
   *
   * Required, and it cannot come from the sibling `FormLabel`. `FormControl`
   * is a Radix `Slot` that injects `id` / `aria-describedby` / `aria-invalid`
   * into its child element — a plain function component neither accepts nor
   * forwards them, so all three are dropped and `FormLabel`'s `htmlFor` points
   * at an id that exists on nothing. The `<legend>` below is what actually
   * names the radio group to a screen reader.
   */
  legend: string
}) {
  const active = options.find((o) => o.value === value)
  const name = useId()
  return (
    <div className="space-y-2">
      {/* Real radio inputs, visually replaced by their labels: keyboard
          navigation, form semantics and screen-reader grouping all come for
          free, which a strip of buttons with `role="radio"` only imitates. */}
      <fieldset
        className="grid gap-1 rounded-lg border bg-muted/40 p-1"
        style={{ gridTemplateColumns: `repeat(${options.length}, 1fr)` }}
      >
        {/* Visually redundant with the FormLabel above it, but that label is
            not programmatically attached to anything — see `legend`. */}
        <legend className="sr-only">{legend}</legend>
        {options.map((option) => {
          const selected = option.value === value
          return (
            <label
              key={option.value}
              className={cn(
                "cursor-pointer rounded-md px-3 py-1.5 text-center text-xs font-medium transition-colors",
                "focus-within:ring-2 focus-within:ring-ring",
                selected
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <input
                type="radio"
                name={name}
                className="sr-only"
                value={option.value}
                checked={selected}
                onChange={() => onChange(option.value)}
              />
              {option.label}
            </label>
          )
        })}
      </fieldset>
      {/* The chosen option's own sentence, not a generic hint: the difference
          between these values is the only thing worth reading here. */}
      {active && (
        <p className="text-xs text-muted-foreground">{active.description}</p>
      )}
    </div>
  )
}

/** Said when the channel write landed and only the grant write did not. */
const GRANTS_FAILED =
  "The channel was saved, but its granted-user list was not. Reopen the channel and add them again."

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
        // Plain strings, not `z.enum`: these columns are VARCHAR on purpose so
        // a new value needs no migration, and a form that refuses to load a
        // value the server accepted is worse than one that round-trips it.
        // The values this control can *produce* are constrained by the control.
        visibility: z.string(),
        default_enabled_for_users: z.boolean(),
        default_agent_scope: z.string(),
        allow_auto_install: z.boolean(),
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
  const storedVisibility = channel?.visibility
  const storedAgentScope = channel?.default_agent_scope

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
      // Two different questions, two different answers. A *stored* value is
      // coerced, and coercion narrows — an unrecognised one must land on the
      // fail-closed branch rather than render as blank or as the widest
      // option. A *new* channel has no stored value to coerce, so it starts
      // from the backend model's own defaults instead.
      visibility:
        storedVisibility === undefined
          ? NEW_CHANNEL_VISIBILITY
          : asVisibility(storedVisibility),
      default_enabled_for_users: channel?.default_enabled_for_users ?? true,
      default_agent_scope:
        storedAgentScope === undefined
          ? NEW_CHANNEL_AGENT_SCOPE
          : asAgentScope(storedAgentScope),
      allow_auto_install: channel?.allow_auto_install ?? true,
    },
  })

  const isRestricted = form.watch("visibility") === VISIBILITY_RESTRICTED

  // ---------------------------------------------------------------------
  // Grants
  //
  // The grant list is a second resource behind a second endpoint, but it is
  // edited in this form and saved by this form's Save button — so it is held
  // as a draft and PUT after the channel write succeeds. On create there is no
  // channel id to PUT against until then, which settles the ordering.
  //
  // Fetched for public channels too (the backend returns them): the rows are
  // the admin's saved allowlist, inert while public, and dropping them on a
  // visibility round-trip would silently revoke everyone.
  // ---------------------------------------------------------------------
  const {
    data: loadedGrants,
    isSuccess: grantsLoaded,
    isError: grantsUnreadable,
  } = useQuery({
    queryKey: ["serverChannelGrants", channel?.id],
    queryFn: () =>
      ServerChannelsService.listChannelGrants({ channelId: channel!.id }),
    enabled: isEdit,
  })

  /**
   * Whether the picker may be shown at all.
   *
   * A create has nothing to load, so it is ready immediately. An edit is NOT
   * ready until the fetch resolves, and that gate is load-bearing rather than
   * cosmetic: `grants` falls back to `[]` while `loadedGrants` is undefined,
   * so a click landing before the response would set `grantDraft` from an
   * empty list and permanently shadow the real one. Save then PUTs that draft
   * to a replace-the-whole-set endpoint and silently revokes every existing
   * grant. The `grantDraft === null` guard covers "never touched"; this covers
   * "touched too early", which is the same failure with a narrower window.
   */
  const grantsReady = !isEdit || grantsLoaded

  /**
   * `null` means "the admin has not touched the picker".
   *
   * That distinction is what keeps a Save from clobbering the grant list: an
   * untouched picker writes nothing at all, so saving an unrelated field while
   * the grants fetch is still in flight cannot PUT an empty list over somebody
   * else's allowlist.
   */
  const [grantDraft, setGrantDraft] = useState<
    UserAllowlistSelectedItem[] | null
  >(null)

  const grantsFromServer: UserAllowlistSelectedItem[] = useMemo(
    () =>
      (loadedGrants ?? []).map((g) => ({
        id: g.user_id,
        userId: g.user_id,
        fallbackLabel: g.full_name || g.email,
      })),
    [loadedGrants],
  )
  const grants = grantDraft ?? grantsFromServer

  /**
   * PUT the grants when — and only when — the picker was edited.
   *
   * Reports failure rather than throwing. The channel write has already
   * succeeded by the time this runs, and letting a failed grant PUT surface as
   * "Failed to create channel" would send the admin back to create a duplicate
   * of a channel that exists. The two writes get two answers.
   */
  const saveGrants = async (channelId: string): Promise<boolean> => {
    if (grantDraft === null) return true
    try {
      await ServerChannelsService.replaceChannelGrants({
        channelId,
        requestBody: { user_ids: grantDraft.map((g) => g.userId) },
      })
      return true
    } catch {
      return false
    }
  }

  const createMutation = useMutation({
    mutationFn: async (body: ServerChannelCreate) => {
      const created = await ServerChannelsService.createChannel({
        requestBody: body,
      })
      return { created, grantsOk: await saveGrants(created.id) }
    },
    onSuccess: ({ created, grantsOk }) => {
      queryClient.invalidateQueries({ queryKey: ["serverChannels"] })
      // A new public channel is available to every user the moment it exists,
      // the creating admin included — their own Settings → Channels must not
      // keep showing the list from before it.
      queryClient.invalidateQueries({ queryKey: ["userChannels"] })
      if (grantsOk) showSuccessToast("Channel created")
      else showErrorToast(GRANTS_FAILED)
      onSaved(created)
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Failed to create channel")),
  })

  const updateMutation = useMutation({
    mutationFn: async (body: ServerChannelUpdate) => {
      await ServerChannelsService.updateChannel({
        channelId: channel!.id,
        requestBody: body,
      })
      return { grantsOk: await saveGrants(channel!.id) }
    },
    onSuccess: ({ grantsOk }) => {
      queryClient.invalidateQueries({ queryKey: ["serverChannels"] })
      queryClient.invalidateQueries({
        queryKey: ["serverChannelSetup", channel!.id],
      })
      queryClient.invalidateQueries({
        queryKey: ["serverChannelGrants", channel!.id],
      })
      // Every user's resolved policy can change with the channel defaults, so
      // a settings page open in another tab must not keep showing the old
      // inherited value.
      queryClient.invalidateQueries({ queryKey: ["userChannels"] })
      if (grantsOk) showSuccessToast("Channel updated")
      else showErrorToast(GRANTS_FAILED)
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
        visibility: values.visibility,
        default_enabled_for_users: values.default_enabled_for_users,
        default_agent_scope: values.default_agent_scope,
        allow_auto_install: values.allow_auto_install,
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
      visibility: values.visibility,
      default_enabled_for_users: values.default_enabled_for_users,
      default_agent_scope: values.default_agent_scope,
      allow_auto_install: values.allow_auto_install,
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

        {/* ---------------------------------------------------------------
            Availability policy. Everything in this block is a *default* for
            users who have never opened Settings → Channels — except
            `visibility`, which is access and applies to everyone. The section
            says so once, so each control does not have to.
            --------------------------------------------------------------- */}
        <div className="space-y-4 rounded-lg border p-3">
          <div className="space-y-0.5">
            <h4 className="text-sm font-medium">Availability</h4>
            <p className="text-xs text-muted-foreground">
              Who may use this channel, and what a user who has never changed
              their own settings gets.
            </p>
          </div>

          <FormField
            control={form.control}
            name="visibility"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Who can use it</FormLabel>
                <FormControl>
                  <SegmentedField
                    value={field.value}
                    onChange={field.onChange}
                    options={VISIBILITY_OPTIONS}
                    legend="Who can use it"
                  />
                </FormControl>
                <FormDescription>{VISIBILITY_HELP}</FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          {isRestricted && grantsUnreadable ? (
            /* Without this branch a failed fetch collapses to `grants = []`
               and the block below asserts NO_GRANTS_WARNING — "nobody can use
               it" — which is a positive claim about who has access, made from
               a request that failed. The admin would then start adding people,
               and every grant they cannot see would be revoked on Save. */
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertDescription>
                Couldn't load who is granted this channel. This is a failed
                request, not an empty list — reopen this dialog before changing
                anything, or you would save over grants you can't see.
              </AlertDescription>
            </Alert>
          ) : isRestricted && !grantsReady ? (
            <Skeleton className="h-9 w-full" />
          ) : isRestricted ? (
            <div className="space-y-2">
              <UserAllowlistPicker
                // Gated on the picker being on screen: without this the search
                // query would run for a channel whose visibility is public and
                // whose picker is not rendered at all.
                enabled={isRestricted}
                includeSelf
                selected={grants}
                label={
                  <Label className="text-xs text-muted-foreground">
                    Granted users
                  </Label>
                }
                searchPlaceholder="Search users to grant..."
                onAdd={(u) =>
                  setGrantDraft([
                    ...grants,
                    {
                      id: u.id,
                      userId: u.id,
                      fallbackLabel: u.full_name || u.email,
                    },
                  ])
                }
                onRemove={(item) =>
                  setGrantDraft(grants.filter((g) => g.userId !== item.userId))
                }
                emptyHint="Nobody granted yet."
              />
              {/* Deliberately a count of the pills we render, and nothing to
                  do with the `count` on the user-search response — that is the
                  page size of the search, not a total, and has been rendered
                  as "N users" by mistake before. */}
              {grants.length === 0 ? (
                <Alert>
                  <AlertTriangle className="h-4 w-4" />
                  <AlertDescription>{NO_GRANTS_WARNING}</AlertDescription>
                </Alert>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Saved with the channel when you press{" "}
                  {isEdit ? "Save" : "Create channel"}.
                </p>
              )}
            </div>
          ) : null}

          <FormField
            control={form.control}
            name="default_enabled_for_users"
            render={({ field }) => (
              <FormItem className="flex items-start justify-between gap-4">
                <div className="space-y-0.5">
                  <FormLabel>On by default</FormLabel>
                  <FormDescription>{DEFAULT_ENABLED_HELP}</FormDescription>
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
            name="default_agent_scope"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Agents reachable by default</FormLabel>
                <FormControl>
                  <SegmentedField
                    value={field.value}
                    onChange={field.onChange}
                    options={AGENT_SCOPE_OPTIONS}
                    legend="Agents reachable by default"
                  />
                </FormControl>
                <FormDescription>{DEFAULT_AGENT_SCOPE_HELP}</FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="allow_auto_install"
            render={({ field }) => (
              <FormItem className="flex items-start justify-between gap-4">
                <div className="space-y-0.5">
                  <FormLabel>Allow auto-install</FormLabel>
                  <FormDescription>{ALLOW_AUTO_INSTALL_HELP}</FormDescription>
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
        </div>

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
