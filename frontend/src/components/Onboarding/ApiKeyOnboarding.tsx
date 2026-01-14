import { useState, KeyboardEvent } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { UsersService } from "@/client"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Sparkles, Key, Loader2, Check, ExternalLink } from "lucide-react"
import useCustomToast from "@/hooks/useCustomToast"

interface ApiKeyOnboardingProps {
  onComplete: () => void
}

export function ApiKeyOnboarding({ onComplete }: ApiKeyOnboardingProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [apiKey, setApiKey] = useState("")
  const [showButton, setShowButton] = useState(false)
  const [saveSuccess, setSaveSuccess] = useState(false)

  const updateMutation = useMutation({
    mutationFn: (key: string) =>
      UsersService.updateAiCredentials({
        requestBody: { anthropic_api_key: key },
      }),
    onSuccess: () => {
      setSaveSuccess(true)
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("API key saved successfully")
      // Brief delay to show success state before transitioning
      setTimeout(() => {
        onComplete()
      }, 1200)
    },
    onError: () => {
      showErrorToast("Failed to save API key. Please check your key and try again.")
    },
  })

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && apiKey.trim()) {
      e.preventDefault()
      if (showButton) {
        handleSave()
      } else {
        setShowButton(true)
      }
    }
  }

  const handleInputChange = (value: string) => {
    setApiKey(value)
    if (value.trim() && !showButton) {
      setShowButton(true)
    } else if (!value.trim()) {
      setShowButton(false)
    }
  }

  const handleSave = () => {
    if (apiKey.trim()) {
      updateMutation.mutate(apiKey.trim())
    }
  }

  return (
    <div className="flex items-center justify-center min-h-[calc(100vh-4rem)]">
      <div className="flex flex-col items-center justify-center text-center max-w-lg px-6">
        {/* Icon */}
        <div className="relative mb-8">
          <div className="rounded-full bg-gradient-to-br from-violet-500/20 to-purple-600/20 p-8">
            <Key className="h-12 w-12 text-violet-500" />
          </div>
          <div className="absolute -top-2 -right-2">
            <Sparkles className="h-6 w-6 text-amber-400 animate-pulse" />
          </div>
        </div>

        {/* Heading */}
        <h2 className="text-2xl font-bold mb-3 bg-gradient-to-r from-violet-600 to-purple-600 bg-clip-text text-transparent">
          Welcome! Let's Get Started
        </h2>
        <p className="text-muted-foreground mb-8 text-base leading-relaxed">
          To power your AI agents, you'll need an Anthropic API key.
          <br />
          This key enables intelligent conversations and automation.
        </p>

        {/* Input */}
        <div className="w-full max-w-md space-y-4">
          <div className="relative">
            <Input
              type="password"
              placeholder="sk-ant-api03-..."
              value={apiKey}
              onChange={(e) => handleInputChange(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={updateMutation.isPending || saveSuccess}
              className="h-12 text-base px-4 pr-12 bg-background/50 border-violet-200 dark:border-violet-800 focus-visible:ring-violet-500"
            />
            {saveSuccess && (
              <div className="absolute right-3 top-1/2 -translate-y-1/2">
                <Check className="h-5 w-5 text-green-500" />
              </div>
            )}
          </div>

          {/* Action Button with Animation */}
          <div
            className={`transition-all duration-500 ease-out ${
              showButton
                ? "opacity-100 translate-y-0"
                : "opacity-0 -translate-y-4 pointer-events-none"
            }`}
          >
            <Button
              onClick={handleSave}
              disabled={!apiKey.trim() || updateMutation.isPending || saveSuccess}
              className="w-full h-12 text-base font-medium bg-gradient-to-r from-violet-600 to-purple-600 hover:from-violet-700 hover:to-purple-700 transition-all duration-300"
            >
              {updateMutation.isPending ? (
                <span className="flex items-center gap-2">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  Saving...
                </span>
              ) : saveSuccess ? (
                <span className="flex items-center gap-2">
                  <Check className="h-5 w-5" />
                  Ready!
                </span>
              ) : (
                <span className="flex items-center gap-2">
                  <Sparkles className="h-5 w-5" />
                  Start Doing Magic
                </span>
              )}
            </Button>
          </div>
        </div>

        {/* Helper Text */}
        <div className="mt-8 space-y-2 text-sm text-muted-foreground">
          <p>
            Get your API key from{" "}
            <a
              href="https://platform.claude.com/settings/keys"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-violet-600 dark:text-violet-400 hover:underline font-medium"
            >
              Claude API Settings
              <ExternalLink className="h-3 w-3" />
            </a>
          </p>
          <p className="text-xs text-muted-foreground/70">
            Or ask your administrator for access credentials
          </p>
        </div>
      </div>
    </div>
  )
}
