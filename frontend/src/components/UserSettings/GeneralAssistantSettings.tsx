import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { ExternalLink, Loader2, Sparkles } from "lucide-react"

import { AgentsService, UsersService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export function GeneralAssistantSettings() {
  const queryClient = useQueryClient()
  const { user: currentUser } = useAuth()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const isEnabled = currentUser?.general_assistant_enabled ?? false

  const { data: agentsData } = useQuery({
    queryKey: ["agents", "general-assistant"],
    queryFn: () => AgentsService.readAgents({ skip: 0, limit: 200 }),
  })

  const gaAgent = agentsData?.data?.find((a) => a.is_general_assistant)

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      UsersService.updateUserMe({ requestBody: { general_assistant_enabled: enabled } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
    onError: handleError.bind(showErrorToast),
  })

  const createMutation = useMutation({
    mutationFn: () => UsersService.generateGeneralAssistant(),
    onSuccess: () => {
      showSuccessToast("General Assistant created successfully!")
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agents", "general-assistant"] })
    },
    onError: handleError.bind(showErrorToast),
  })

  return (
    <Card className="max-w-lg">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-violet-500" />
          General Assistant
        </CardTitle>
        <CardDescription>
          Your platform assistant — helps set up agents, workspaces, and automations.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-3">
          <Switch
            id="ga-enabled"
            checked={isEnabled}
            disabled={toggleMutation.isPending}
            onCheckedChange={(checked) => toggleMutation.mutate(checked)}
          />
          <Label htmlFor="ga-enabled" className="cursor-pointer">
            {isEnabled ? "Enabled" : "Disabled"}
          </Label>
          {toggleMutation.isPending && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          )}
        </div>

        {isEnabled && (
          <div className="pt-1">
            {gaAgent ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <span className="inline-flex items-center gap-1 rounded-full bg-violet-100 dark:bg-violet-900/40 px-2 py-0.5 text-xs font-medium text-violet-700 dark:text-violet-300">
                  <Sparkles className="h-3 w-3" />
                  General Assistant is active
                </span>
                <Link
                  to="/agent/$agentId"
                  params={{ agentId: gaAgent.id }}
                  className="inline-flex items-center gap-1 text-xs text-violet-600 dark:text-violet-400 hover:underline"
                >
                  View agent
                  <ExternalLink className="h-3 w-3" />
                </Link>
              </div>
            ) : (
              <Button
                size="sm"
                onClick={() => createMutation.mutate()}
                disabled={createMutation.isPending}
                className="bg-violet-600 hover:bg-violet-700 text-white"
              >
                {createMutation.isPending ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                    Generating...
                  </>
                ) : (
                  <>
                    <Sparkles className="h-3.5 w-3.5 mr-1.5" />
                    Generate Assistant
                  </>
                )}
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
