import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  NotificationSettingsService,
  type NotificationSettingItem,
} from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"

export function NotificationSettings() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data, isLoading } = useQuery({
    queryKey: ["notification-settings"],
    queryFn: () => NotificationSettingsService.readNotificationSettings(),
  })

  const updateMutation = useMutation({
    mutationFn: ({
      notificationType,
      emailEnabled,
    }: {
      notificationType: string
      emailEnabled: boolean
    }) =>
      NotificationSettingsService.updateNotificationSetting({
        notificationType,
        requestBody: { email_enabled: emailEnabled },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notification-settings"] })
      showSuccessToast("Notification preference updated")
    },
    onError: () => {
      // Revert the toggled row by refetching the source of truth.
      queryClient.invalidateQueries({ queryKey: ["notification-settings"] })
      showErrorToast("Failed to update notification preference")
    },
  })

  const items: NotificationSettingItem[] = data?.data ?? []

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>Notifications</CardTitle>
        <CardDescription>
          Choose which system notifications you receive by email.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No notification types available.
          </p>
        ) : (
          items.map((item) => {
            const pending =
              updateMutation.isPending &&
              updateMutation.variables?.notificationType ===
                item.notification_type
            const switchId = `notification-${item.notification_type}`
            const descId = `${switchId}-description`
            return (
              <div
                key={item.notification_type}
                className="flex items-start justify-between gap-4"
              >
                <div className="space-y-0.5">
                  <Label htmlFor={switchId} className="text-sm font-medium">
                    {item.label}
                  </Label>
                  <p
                    id={descId}
                    className="text-sm text-muted-foreground"
                  >
                    {item.description}
                  </p>
                </div>
                <Switch
                  id={switchId}
                  aria-describedby={descId}
                  checked={item.email_enabled}
                  disabled={pending}
                  onCheckedChange={(checked) =>
                    updateMutation.mutate({
                      notificationType: item.notification_type,
                      emailEnabled: checked,
                    })
                  }
                />
              </div>
            )
          })
        )}
      </CardContent>
    </Card>
  )
}
