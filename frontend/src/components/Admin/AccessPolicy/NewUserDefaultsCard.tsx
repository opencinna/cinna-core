import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  ROLE_AGENT_DEVELOPER,
  ROLE_AGENT_USER,
  userRoleLabel,
} from "@/utils/userRoles"
import {
  pendingConfigField,
  useServerConfig,
  useServerConfigUpdate,
} from "./accessPolicy"

/**
 * What an account becomes the moment it is created.
 *
 * Only two of the three roles may be a default: `admin` is granted, never
 * defaulted into. Both the identifiers and the labels come from the shared
 * table so this card and the users page cannot drift apart.
 */
export function NewUserDefaultsCard() {
  const { data: config, isError, error, refetch } = useServerConfig()
  const updateMutation = useServerConfigUpdate()
  const pendingField = pendingConfigField(updateMutation)

  const defaultRole = config?.default_user_role ?? ROLE_AGENT_USER
  const inviteIncludeDesktop = config?.invite_include_desktop_default ?? true

  const body = () => {
    // Only when there is nothing to render — a failed background refetch keeps
    // the last good values on screen rather than blanking the card.
    if (isError && config === undefined) {
      return (
        <QueryErrorAlert
          error={error}
          fallback="Couldn't read the new-user defaults."
          onRetry={() => refetch()}
        />
      )
    }

    if (!config) {
      return (
        <div className="space-y-4">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-11 w-full" />
        </div>
      )
    }

    return (
      <div className="space-y-4">
        {/* See RegistrationCard: the control column only fits beside its
            explanation once the card is wider than a half of ~1000px. */}
        <div className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between xl:gap-4">
          <div className="min-w-0">
            <Label htmlFor="default-user-role" className="text-sm font-medium">
              Default role
            </Label>
            <p className="text-xs text-muted-foreground">
              {defaultRole === ROLE_AGENT_DEVELOPER
                ? "New accounts can build and configure agents."
                : "New accounts can use agents that were shared with them."}
            </p>
          </div>
          <Select
            value={defaultRole}
            onValueChange={(value) =>
              updateMutation.mutate({ default_user_role: value })
            }
            disabled={pendingField === "default_user_role"}
          >
            <SelectTrigger
              id="default-user-role"
              className="w-full xl:w-[240px] xl:shrink-0"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ROLE_AGENT_USER}>
                {userRoleLabel(ROLE_AGENT_USER)}
              </SelectItem>
              <SelectItem value={ROLE_AGENT_DEVELOPER}>
                {userRoleLabel(ROLE_AGENT_DEVELOPER)}
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <Label
              htmlFor="invite-include-desktop"
              className="text-sm font-medium"
            >
              Offer Cinna Desktop in invitations
            </Label>
            <p className="text-xs text-muted-foreground">
              Pre-ticks the desktop download block when you write an invitation.
            </p>
          </div>
          <div className="shrink-0">
            <Switch
              id="invite-include-desktop"
              checked={inviteIncludeDesktop}
              onCheckedChange={(checked) =>
                updateMutation.mutate({
                  invite_include_desktop_default: checked,
                })
              }
              disabled={pendingField === "invite_include_desktop_default"}
              aria-label="Offer Cinna Desktop in invitations"
            />
          </div>
        </div>
      </div>
    )
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>New user defaults</CardTitle>
        <CardDescription>What every new account starts as.</CardDescription>
      </CardHeader>
      <CardContent>{body()}</CardContent>
    </Card>
  )
}
