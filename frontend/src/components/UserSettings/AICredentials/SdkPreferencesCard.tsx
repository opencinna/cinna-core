import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { MessageCircle, Pencil, Sparkles, Wrench } from "lucide-react"
import { useState } from "react"

import type { AICredentialPublic, UserPublicWithAICredentials } from "@/client"
import { AiCredentialsService, UsersService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { AiFunctionsEditDialog } from "./AiFunctionsEditDialog"
import {
  buildModeSummary,
  composeSDKId,
  extractEngine,
  USE_DEFAULT_SENTINEL,
} from "./credentialTypes"
import { SDKModeEditDialog } from "./SDKModeEditDialog"

interface SdkPreferencesCardProps {
  status: UserPublicWithAICredentials | undefined
  isLoading: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
  credentials: AICredentialPublic[]
}

interface SummaryRowProps {
  icon: React.ReactNode
  iconBg: string
  label: string
  value: string
  secondary: string[]
  editLabel: string
  onEdit: () => void
}

/** One P2 summary row: what this setting is, what it resolves to, one Pencil. */
function SummaryRow({
  icon,
  iconBg,
  label,
  value,
  secondary,
  editLabel,
  onEdit,
}: SummaryRowProps) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
      <div className="flex items-start gap-3 min-w-0">
        <div
          className={`flex items-center justify-center w-7 h-7 rounded-lg shrink-0 mt-0.5 ${iconBg}`}
        >
          {icon}
        </div>
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground mb-0.5">{label}</p>
          <p className="text-sm font-medium truncate">{value}</p>
          {secondary.map((line) => (
            <p key={line} className="text-xs text-muted-foreground truncate">
              {line}
            </p>
          ))}
        </div>
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            aria-label={editLabel}
            onClick={onEdit}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          {editLabel}
        </TooltipContent>
      </Tooltip>
    </div>
  )
}

const AI_FUNCTIONS_LABELS: Record<string, string> = {
  system: "System (default)",
  "personal:anthropic": "Personal Anthropic",
  "personal:openai": "Personal OpenAI",
}

/**
 * "Default SDK Preferences" — what a new environment runs on, and what the
 * platform uses for its own AI calls.
 *
 * Three identical P2 summary rows, each edited in a dialog. The third row used
 * to be an auto-saving `Select` with two conditional pickers indented beneath
 * it, which gave the card two interaction models and an inline two-field form;
 * both are now inside `AiFunctionsEditDialog`.
 *
 * Every value here is read straight from the server's `status`. The card used
 * to seed local state with hardcoded defaults ("claude-code", "Use Default",
 * no override) and render them while the request was still in flight, so a
 * loading card stated a configuration that might not be the user's.
 */
export function SdkPreferencesCard({
  status,
  isLoading,
  isError,
  error,
  onRetry,
  credentials,
}: SdkPreferencesCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [editingMode, setEditingMode] = useState<
    "conversation" | "building" | null
  >(null)
  const [isAiFunctionsOpen, setIsAiFunctionsOpen] = useState(false)

  const conversation = {
    engine: extractEngine(status?.default_sdk_conversation),
    credentialId:
      status?.default_ai_credential_conversation_id ?? USE_DEFAULT_SENTINEL,
    modelOverride: status?.default_model_override_conversation ?? "",
  }
  const building = {
    engine: extractEngine(status?.default_sdk_building),
    credentialId:
      status?.default_ai_credential_building_id ?? USE_DEFAULT_SENTINEL,
    modelOverride: status?.default_model_override_building ?? "",
  }

  // Per-engine resolution: the endpoint answers "which default would this
  // engine actually use", which is not derivable from the list on its own.
  const { data: resolvedConversation } = useQuery({
    queryKey: ["resolveDefaultCredential", conversation.engine],
    queryFn: () =>
      AiCredentialsService.resolveDefaultCredential({
        sdkEngine: conversation.engine,
      }),
    enabled: !!status && conversation.credentialId === USE_DEFAULT_SENTINEL,
  })
  const { data: resolvedBuilding } = useQuery({
    queryKey: ["resolveDefaultCredential", building.engine],
    queryFn: () =>
      AiCredentialsService.resolveDefaultCredential({
        sdkEngine: building.engine,
      }),
    enabled: !!status && building.credentialId === USE_DEFAULT_SENTINEL,
  })

  const updateMutation = useMutation({
    mutationFn: (
      body: Parameters<typeof UsersService.updateUserMe>[0]["requestBody"],
    ) => UsersService.updateUserMe({ requestBody: body }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
      queryClient.invalidateQueries({ queryKey: ["resolveDefaultCredential"] })
      showSuccessToast("SDK preferences saved")
    },
    onError: () => showErrorToast("Failed to save SDK preferences"),
  })

  const handleModeSave = (
    mode: "conversation" | "building",
    engine: string,
    credentialId: string,
    modelOverride: string,
  ) => {
    const credId = credentialId === USE_DEFAULT_SENTINEL ? null : credentialId
    const selectedCred = credentials.find((c) => c.id === credentialId) ?? null
    const sdkId = composeSDKId(engine, selectedCred?.type ?? null)
    const body =
      mode === "conversation"
        ? {
            default_sdk_conversation: sdkId,
            default_ai_credential_conversation_id: credId,
            default_model_override_conversation: modelOverride.trim() || null,
          }
        : {
            default_sdk_building: sdkId,
            default_ai_credential_building_id: credId,
            default_model_override_building: modelOverride.trim() || null,
          }

    updateMutation.mutate(body, { onSuccess: () => setEditingMode(null) })
  }

  const handleAiFunctionsSave = (
    provider: string,
    credentialId: string | null,
  ) => {
    updateMutation.mutate(
      {
        default_ai_functions_sdk: provider,
        default_ai_functions_credential_id: credentialId,
      },
      { onSuccess: () => setIsAiFunctionsOpen(false) },
    )
  }

  const renderRows = (loaded: UserPublicWithAICredentials) => {
    const convSummary = buildModeSummary(
      conversation.engine,
      conversation.credentialId,
      conversation.modelOverride,
      credentials,
      resolvedConversation,
    )
    const buildSummary = buildModeSummary(
      building.engine,
      building.credentialId,
      building.modelOverride,
      credentials,
      resolvedBuilding,
    )

    const aiFunctionsSdk = loaded.default_ai_functions_sdk || "system"
    const aiFunctionsType =
      aiFunctionsSdk === "personal:openai" ? "openai" : "anthropic"
    const aiFunctionsCred =
      credentials.find(
        (c) => c.id === loaded.default_ai_functions_credential_id,
      ) ??
      credentials.find((c) => c.type === aiFunctionsType && c.is_default) ??
      null
    const aiFunctionsSecondary =
      aiFunctionsSdk === "system"
        ? "Titles, schedules and suggestions"
        : (aiFunctionsCred?.name ??
          `No ${aiFunctionsType === "openai" ? "OpenAI" : "Anthropic"} credential yet`)

    return (
      <>
        <SummaryRow
          icon={<MessageCircle className="h-3.5 w-3.5 text-blue-500" />}
          iconBg="bg-blue-500/10"
          label="Conversation"
          value={convSummary.engine}
          secondary={[
            convSummary.credential,
            ...(convSummary.model ? [`Model: ${convSummary.model}`] : []),
          ]}
          editLabel="Edit conversation defaults"
          onEdit={() => setEditingMode("conversation")}
        />
        <SummaryRow
          icon={<Wrench className="h-3.5 w-3.5 text-orange-500" />}
          iconBg="bg-orange-500/10"
          label="Building"
          value={buildSummary.engine}
          secondary={[
            buildSummary.credential,
            ...(buildSummary.model ? [`Model: ${buildSummary.model}`] : []),
          ]}
          editLabel="Edit building defaults"
          onEdit={() => setEditingMode("building")}
        />
        <SummaryRow
          icon={<Sparkles className="h-3.5 w-3.5 text-purple-500" />}
          iconBg="bg-purple-500/10"
          label="AI Functions"
          value={AI_FUNCTIONS_LABELS[aiFunctionsSdk] ?? aiFunctionsSdk}
          secondary={[aiFunctionsSecondary]}
          editLabel="Edit AI Functions provider"
          onEdit={() => setIsAiFunctionsOpen(true)}
        />
      </>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Default SDK Preferences</CardTitle>
        <CardDescription>
          Default AI engine, credential, and model for new environments.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* `isError` is answered before `!status`, so a failed request never
            renders as a card full of plausible-looking defaults. */}
        {isError ? (
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load your SDK preferences"
            onRetry={onRetry}
          />
        ) : isLoading || !status ? (
          <>
            <Skeleton className="h-[68px] w-full rounded-md" />
            <Skeleton className="h-[68px] w-full rounded-md" />
            <Skeleton className="h-[68px] w-full rounded-md" />
          </>
        ) : (
          renderRows(status)
        )}
      </CardContent>

      {editingMode && (
        <SDKModeEditDialog
          open
          onOpenChange={(next) => {
            if (!next) setEditingMode(null)
          }}
          mode={editingMode}
          engine={
            editingMode === "conversation"
              ? conversation.engine
              : building.engine
          }
          credentialId={
            editingMode === "conversation"
              ? conversation.credentialId
              : building.credentialId
          }
          modelOverride={
            editingMode === "conversation"
              ? conversation.modelOverride
              : building.modelOverride
          }
          credentials={credentials}
          onSave={(engine, credentialId, modelOverride) =>
            handleModeSave(editingMode, engine, credentialId, modelOverride)
          }
          isSaving={updateMutation.isPending}
        />
      )}

      {isAiFunctionsOpen && status && (
        <AiFunctionsEditDialog
          open
          onOpenChange={setIsAiFunctionsOpen}
          provider={status.default_ai_functions_sdk || "system"}
          credentialId={status.default_ai_functions_credential_id ?? null}
          credentials={credentials}
          onSave={handleAiFunctionsSave}
          isSaving={updateMutation.isPending}
        />
      )}
    </Card>
  )
}
