import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { HelpCircle } from "lucide-react"
import { useMemo } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { UsersService, type UserUpdateMe } from "@/client"
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
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { SearchableSelect } from "@/components/Common/SearchableSelect"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

// Curated communication-language list (primary subtag → human label). The
// detected `navigator.language` primary subtag is allowed even if absent here.
const LANGUAGE_OPTIONS: { value: string; label: string }[] = [
  { value: "en", label: "English" },
  { value: "de", label: "German" },
  { value: "fr", label: "French" },
  { value: "es", label: "Spanish" },
  { value: "it", label: "Italian" },
  { value: "pt", label: "Portuguese" },
  { value: "nl", label: "Dutch" },
  { value: "pl", label: "Polish" },
  { value: "ru", label: "Russian" },
  { value: "uk", label: "Ukrainian" },
  { value: "tr", label: "Turkish" },
  { value: "ar", label: "Arabic" },
  { value: "zh", label: "Chinese" },
  { value: "ja", label: "Japanese" },
  { value: "ko", label: "Korean" },
  { value: "hi", label: "Hindi" },
]

// Curated BCP-47 formatting-locale list. The detected `navigator.language`
// full tag is allowed even if absent here.
const LOCALE_OPTIONS: { value: string; label: string }[] = [
  { value: "en-US", label: "English (United States) — en-US" },
  { value: "en-GB", label: "English (United Kingdom) — en-GB" },
  { value: "de-DE", label: "German (Germany) — de-DE" },
  { value: "fr-FR", label: "French (France) — fr-FR" },
  { value: "es-ES", label: "Spanish (Spain) — es-ES" },
  { value: "it-IT", label: "Italian (Italy) — it-IT" },
  { value: "pt-BR", label: "Portuguese (Brazil) — pt-BR" },
  { value: "pt-PT", label: "Portuguese (Portugal) — pt-PT" },
  { value: "nl-NL", label: "Dutch (Netherlands) — nl-NL" },
  { value: "pl-PL", label: "Polish (Poland) — pl-PL" },
  { value: "ru-RU", label: "Russian (Russia) — ru-RU" },
  { value: "ja-JP", label: "Japanese (Japan) — ja-JP" },
  { value: "zh-CN", label: "Chinese (China) — zh-CN" },
]

// Fallback timezone list for browsers without Intl.supportedValuesOf.
const FALLBACK_TIMEZONES = [
  "UTC",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "Europe/Madrid",
  "Europe/Moscow",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Sao_Paulo",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Australia/Sydney",
]

const CONVERSATION_STYLE_OPTIONS = [
  { value: "ai_default", label: "AI Default (no adjustments)" },
  { value: "concise_direct", label: "Concise and direct" },
  { value: "friendly_chatty", label: "Friendly and chatty" },
] as const

const formSchema = z.object({
  timezone: z.string().max(64).optional().or(z.literal("")),
  language: z.string().max(64).optional().or(z.literal("")),
  locale: z.string().max(64).optional().or(z.literal("")),
  conversation_style: z
    .enum(["ai_default", "concise_direct", "friendly_chatty"])
    .optional(),
})

type FormData = z.infer<typeof formSchema>

/** Build a de-duplicated, sorted list of {value,label} options, injecting the
 *  current/detected value at the top if it's not already present. */
const withExtra = (
  base: { value: string; label: string }[],
  extra: string | null | undefined,
) => {
  if (!extra || base.some((o) => o.value === extra)) return base
  return [{ value: extra, label: extra }, ...base]
}

const UserPreferences = () => {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { user: currentUser } = useAuth()

  const detectedTimezone =
    typeof Intl !== "undefined" && typeof Intl.DateTimeFormat === "function"
      ? Intl.DateTimeFormat().resolvedOptions().timeZone
      : undefined
  const detectedLocale =
    typeof navigator !== "undefined" ? navigator.language : undefined
  const detectedLanguage = detectedLocale
    ? detectedLocale.split("-")[0]
    : undefined

  // Leading "Not set" entry (empty value) so the searchable dropdowns can clear
  // the preference — mirrors the prior shadcn-Select "Not set" sentinel item.
  const NOT_SET = { value: "", label: "Not set" }

  const timezoneOptions = useMemo(() => {
    let zones: string[]
    const supported = (
      Intl as unknown as {
        supportedValuesOf?: (key: string) => string[]
      }
    ).supportedValuesOf
    if (typeof Intl !== "undefined" && typeof supported === "function") {
      try {
        zones = supported("timeZone")
      } catch {
        zones = FALLBACK_TIMEZONES
      }
    } else {
      zones = FALLBACK_TIMEZONES
    }
    const set = new Set(zones)
    if (detectedTimezone) set.add(detectedTimezone)
    if (currentUser?.timezone) set.add(currentUser.timezone)
    return [
      NOT_SET,
      ...Array.from(set)
        .sort()
        .map((tz) => ({ value: tz, label: tz })),
    ]
  }, [detectedTimezone, currentUser?.timezone])

  const languageOptions = useMemo(
    () => [
      NOT_SET,
      ...withExtra(withExtra(LANGUAGE_OPTIONS, detectedLanguage), currentUser?.language),
    ],
    [detectedLanguage, currentUser?.language],
  )
  const localeOptions = useMemo(
    () => [
      NOT_SET,
      ...withExtra(withExtra(LOCALE_OPTIONS, detectedLocale), currentUser?.locale),
    ],
    [detectedLocale, currentUser?.locale],
  )

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: {
      timezone: currentUser?.timezone ?? "",
      language: currentUser?.language ?? "",
      locale: currentUser?.locale ?? "",
      conversation_style:
        (currentUser?.conversation_style as FormData["conversation_style"]) ??
        "ai_default",
    },
  })

  const mutation = useMutation({
    mutationFn: (data: UserUpdateMe) =>
      UsersService.updateUserMe({ requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Preferences updated")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries()
    },
  })

  // Auto-save: persist a single field as soon as it changes, skipping the
  // mutation when the value is unchanged from what the server already holds.
  const persistNullable = (
    key: "timezone" | "language" | "locale",
    raw: string,
  ) => {
    const value = raw || null
    const current = (currentUser?.[key] as string | null | undefined) ?? null
    if (value !== current) mutation.mutate({ [key]: value })
  }

  const persistConversationStyle = (
    value: NonNullable<FormData["conversation_style"]>,
  ) => {
    if (value !== (currentUser?.conversation_style ?? "ai_default")) {
      mutation.mutate({ conversation_style: value })
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Communication &amp; Locale</CardTitle>
        <CardDescription>
          How your agents communicate with you. These apply to every agent you
          own.
        </CardDescription>
      </CardHeader>
      <Form {...form}>
        <div>
          <CardContent className="space-y-4">
            <FormField
              control={form.control}
              name="conversation_style"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between gap-4 space-y-0">
                  <div className="space-y-0.5">
                    <FormLabel>Conversation style</FormLabel>
                    <FormDescription>
                      Adjusts the tone your agents use. "AI Default" applies no
                      adjustment.
                    </FormDescription>
                    <FormMessage />
                  </div>
                  <Select
                    value={field.value ?? "ai_default"}
                    onValueChange={(v) => {
                      field.onChange(v)
                      persistConversationStyle(
                        v as NonNullable<FormData["conversation_style"]>,
                      )
                    }}
                  >
                    <FormControl>
                      <SelectTrigger className="w-[200px] shrink-0">
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {CONVERSATION_STYLE_OPTIONS.map((o) => (
                        <SelectItem key={o.value} value={o.value}>
                          {o.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="language"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between gap-4 space-y-0">
                  <div className="space-y-0.5">
                    <FormLabel className="flex items-center gap-1.5">
                      Language
                      <TooltipProvider delayDuration={200}>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span
                              tabIndex={0}
                              className="inline-flex cursor-help text-muted-foreground"
                              aria-label="More information about Language"
                            >
                              <HelpCircle className="h-3.5 w-3.5" />
                            </span>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-xs">
                            By default the agent replies in whatever language you
                            wrote to it in. This setting only applies as a
                            fallback when that language is unclear.
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </FormLabel>
                    <FormDescription>
                      What language the agent talks to you in.
                    </FormDescription>
                    <FormMessage />
                  </div>
                  <SearchableSelect
                    value={field.value ?? ""}
                    onChange={(v) => {
                      field.onChange(v)
                      persistNullable("language", v)
                    }}
                    options={languageOptions}
                    placeholder="Not set"
                    searchPlaceholder="Search languages..."
                    emptyText="No matching language."
                    className="w-[200px] shrink-0"
                  />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="locale"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between gap-4 space-y-0">
                  <div className="space-y-0.5">
                    <FormLabel>Locale</FormLabel>
                    <FormDescription>
                      How dates, times, and numbers are formatted (distinct from
                      Language).
                    </FormDescription>
                    <FormMessage />
                  </div>
                  <SearchableSelect
                    value={field.value ?? ""}
                    onChange={(v) => {
                      field.onChange(v)
                      persistNullable("locale", v)
                    }}
                    options={localeOptions}
                    placeholder="Not set"
                    searchPlaceholder="Search locales..."
                    emptyText="No matching locale."
                    className="w-[200px] shrink-0"
                  />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="timezone"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between gap-4 space-y-0">
                  <div className="space-y-0.5">
                    <FormLabel>Timezone</FormLabel>
                    <FormDescription>
                      The IANA timezone your agents express dates and times in.
                    </FormDescription>
                    <FormMessage />
                  </div>
                  <SearchableSelect
                    value={field.value ?? ""}
                    onChange={(v) => {
                      field.onChange(v)
                      persistNullable("timezone", v)
                    }}
                    options={timezoneOptions}
                    placeholder="Not set"
                    searchPlaceholder="Search timezones..."
                    emptyText="No matching timezone."
                    className="w-[200px] shrink-0"
                  />
                </FormItem>
              )}
            />
          </CardContent>
        </div>
      </Form>
    </Card>
  )
}

export default UserPreferences
