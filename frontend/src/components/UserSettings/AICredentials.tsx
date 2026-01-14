import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Trash2, AlertCircle } from "lucide-react"
import { UsersService } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Alert, AlertDescription } from "@/components/ui/alert"
import useCustomToast from "@/hooks/useCustomToast"

// SDK options
const SDK_OPTIONS = [
  { id: "claude-code/anthropic", name: "Anthropic Claude", requiredKey: "anthropic" },
  { id: "claude-code/minimax", name: "MiniMax M2", requiredKey: "minimax" },
]

function getSDKDisplayName(sdkId: string | null | undefined): string {
  const sdk = SDK_OPTIONS.find(s => s.id === sdkId)
  return sdk?.name || "Anthropic Claude"
}

export function AICredentialsSettings() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [anthropicKey, setAnthropicKey] = useState("")
  const [minimaxKey, setMinimaxKey] = useState("")

  // Get current status
  const { data: status } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
  })

  // Update Anthropic mutation
  const updateAnthropicMutation = useMutation({
    mutationFn: (key: string) =>
      UsersService.updateAiCredentials({
        requestBody: { anthropic_api_key: key }
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      setAnthropicKey("")
      showSuccessToast("Anthropic API key updated successfully")
    },
    onError: () => {
      showErrorToast("Failed to update Anthropic API key")
    },
  })

  // Update MiniMax mutation
  const updateMinimaxMutation = useMutation({
    mutationFn: (key: string) =>
      UsersService.updateAiCredentials({
        requestBody: { minimax_api_key: key }
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      setMinimaxKey("")
      showSuccessToast("MiniMax API key updated successfully")
    },
    onError: () => {
      showErrorToast("Failed to update MiniMax API key")
    },
  })

  // Delete Anthropic mutation
  const deleteAnthropicMutation = useMutation({
    mutationFn: () =>
      UsersService.updateAiCredentials({
        requestBody: { anthropic_api_key: "" }
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("Anthropic API key deleted successfully")
    },
    onError: () => {
      showErrorToast("Failed to delete Anthropic API key")
    },
  })

  // Delete MiniMax mutation
  const deleteMinimaxMutation = useMutation({
    mutationFn: () =>
      UsersService.updateAiCredentials({
        requestBody: { minimax_api_key: "" }
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("MiniMax API key deleted successfully")
    },
    onError: () => {
      showErrorToast("Failed to delete MiniMax API key")
    },
  })

  // Update SDK preferences mutation
  const updateSdkMutation = useMutation({
    mutationFn: (data: { default_sdk_conversation?: string; default_sdk_building?: string }) =>
      UsersService.updateUserMe({ requestBody: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
      showSuccessToast("SDK preferences updated successfully")
    },
    onError: () => {
      showErrorToast("Failed to update SDK preferences")
    },
  })

  // Check if required API key is available for a given SDK
  const hasRequiredKey = (sdkId: string): boolean => {
    const sdk = SDK_OPTIONS.find(s => s.id === sdkId)
    if (!sdk) return false
    if (sdk.requiredKey === "anthropic") return status?.has_anthropic_api_key ?? false
    if (sdk.requiredKey === "minimax") return status?.has_minimax_api_key ?? false
    return false
  }

  // Get missing key warning for selected SDKs
  const getMissingKeyWarning = (): string | null => {
    const warnings: string[] = []
    const convSdk = status?.default_sdk_conversation || "claude-code/anthropic"
    const buildSdk = status?.default_sdk_building || "claude-code/anthropic"

    if (!hasRequiredKey(convSdk)) {
      warnings.push(`${getSDKDisplayName(convSdk)} (Conversation mode)`)
    }
    if (!hasRequiredKey(buildSdk) && buildSdk !== convSdk) {
      warnings.push(`${getSDKDisplayName(buildSdk)} (Building mode)`)
    } else if (!hasRequiredKey(buildSdk) && buildSdk === convSdk && !warnings.length) {
      warnings.push(`${getSDKDisplayName(buildSdk)} (Building mode)`)
    }

    if (warnings.length === 0) return null
    return `Missing API key for: ${warnings.join(", ")}`
  }

  const missingKeyWarning = getMissingKeyWarning()

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* Left Card - API Credentials */}
      <Card>
        <CardHeader>
          <CardTitle>AI Services Credentials</CardTitle>
          <CardDescription>
            Manage your API keys for AI services. These are used by your agents.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Anthropic API Key */}
          <div className="space-y-2">
            <Label htmlFor="anthropic-key">Anthropic API Key</Label>
            <div className="flex gap-2">
              <Input
                id="anthropic-key"
                type="password"
                placeholder={status?.has_anthropic_api_key ? "••••••••••••••••" : "sk-ant-..."}
                value={anthropicKey}
                onChange={(e) => setAnthropicKey(e.target.value)}
              />
              {anthropicKey && (
                <Button
                  onClick={() => updateAnthropicMutation.mutate(anthropicKey)}
                  disabled={updateAnthropicMutation.isPending}
                >
                  {status?.has_anthropic_api_key ? "Update" : "Save"}
                </Button>
              )}
              {status?.has_anthropic_api_key && (
                <Button
                  variant="destructive"
                  size="icon"
                  onClick={() => deleteAnthropicMutation.mutate()}
                  disabled={deleteAnthropicMutation.isPending}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              Get your API key from{" "}
              <a
                href="https://console.anthropic.com/settings/keys"
                target="_blank"
                rel="noopener noreferrer"
                className="underline"
              >
                Anthropic Console
              </a>
            </p>
          </div>

          {/* MiniMax API Key */}
          <div className="space-y-2">
            <Label htmlFor="minimax-key">MiniMax API Key</Label>
            <div className="flex gap-2">
              <Input
                id="minimax-key"
                type="password"
                placeholder={status?.has_minimax_api_key ? "••••••••••••••••" : "Enter MiniMax API key"}
                value={minimaxKey}
                onChange={(e) => setMinimaxKey(e.target.value)}
              />
              {minimaxKey && (
                <Button
                  onClick={() => updateMinimaxMutation.mutate(minimaxKey)}
                  disabled={updateMinimaxMutation.isPending}
                >
                  {status?.has_minimax_api_key ? "Update" : "Save"}
                </Button>
              )}
              {status?.has_minimax_api_key && (
                <Button
                  variant="destructive"
                  size="icon"
                  onClick={() => deleteMinimaxMutation.mutate()}
                  disabled={deleteMinimaxMutation.isPending}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              Get your API key from{" "}
              <a
                href="https://platform.minimax.io/user-center/basic-information/interface-key"
                target="_blank"
                rel="noopener noreferrer"
                className="underline"
              >
                MiniMax Platform
              </a>
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Right Card - Default SDK Preferences */}
      <Card>
        <CardHeader>
          <CardTitle>Default SDK Preferences</CardTitle>
          <CardDescription>
            Select default AI SDKs for new environments. These can be overridden per environment.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Validation Warning */}
          {missingKeyWarning && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{missingKeyWarning}</AlertDescription>
            </Alert>
          )}

          {/* Conversation Mode SDK */}
          <div className="space-y-2">
            <Label htmlFor="sdk-conversation">Conversation Mode SDK</Label>
            <Select
              value={status?.default_sdk_conversation || "claude-code/anthropic"}
              onValueChange={(value) => updateSdkMutation.mutate({ default_sdk_conversation: value })}
              disabled={updateSdkMutation.isPending}
            >
              <SelectTrigger id="sdk-conversation">
                <SelectValue placeholder="Select SDK" />
              </SelectTrigger>
              <SelectContent>
                {SDK_OPTIONS.map((sdk) => (
                  <SelectItem key={sdk.id} value={sdk.id}>
                    {sdk.name}
                    {!hasRequiredKey(sdk.id) && " (API key required)"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              SDK used when chatting with agents
            </p>
          </div>

          {/* Building Mode SDK */}
          <div className="space-y-2">
            <Label htmlFor="sdk-building">Building Mode SDK</Label>
            <Select
              value={status?.default_sdk_building || "claude-code/anthropic"}
              onValueChange={(value) => updateSdkMutation.mutate({ default_sdk_building: value })}
              disabled={updateSdkMutation.isPending}
            >
              <SelectTrigger id="sdk-building">
                <SelectValue placeholder="Select SDK" />
              </SelectTrigger>
              <SelectContent>
                {SDK_OPTIONS.map((sdk) => (
                  <SelectItem key={sdk.id} value={sdk.id}>
                    {sdk.name}
                    {!hasRequiredKey(sdk.id) && " (API key required)"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              SDK used when agents build and execute tasks
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
