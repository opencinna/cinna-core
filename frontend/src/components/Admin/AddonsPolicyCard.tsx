import { Blocks } from "lucide-react"

import type { ServerConfigUpdate } from "@/client"
import {
  pendingConfigField,
  useServerConfig,
  useServerConfigUpdate,
} from "@/components/Admin/AccessPolicy/accessPolicy"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
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

type AddonsFlag = keyof Pick<
  ServerConfigUpdate,
  | "addons_external_directories"
  | "addons_custom_mcp"
  | "addons_stdio_mcp"
  | "addons_plugin_hooks"
>

/** The four flags in the order the card offers them (UI spec S12). */
const ROWS: readonly { field: AddonsFlag; label: string; help: string }[] = [
  {
    field: "addons_external_directories",
    label: "Public MCP directories",
    help: "Show servers from opencinna.io and the public MCP registry when users add an addon.",
  },
  {
    field: "addons_custom_mcp",
    label: "Custom MCP servers",
    help: "Let users connect an MCP server by typing its URL or command.",
  },
  {
    field: "addons_stdio_mcp",
    label: "Local MCP servers",
    help: "Let users install MCP servers that run as a process on their computer.",
  },
  {
    field: "addons_plugin_hooks",
    label: "Plugin hooks",
    help: "Let hooks shipped inside plugins run; when off, plugins install with their hooks disabled.",
  },
]

/**
 * S12 — which kinds of addons Cinna Desktop may let users add beyond what is
 * published here. Auto-save switch rows, the `SignInMethodsCard` skeleton.
 */
export function AddonsPolicyCard() {
  const { data: config, isError, error, refetch } = useServerConfig()
  const updateMutation = useServerConfigUpdate()
  // Only the switch that was clicked goes dead while it saves.
  const pendingField = pendingConfigField(updateMutation)

  const body = () => {
    if (isError && !config) {
      return (
        <QueryErrorAlert
          error={error}
          fallback="Couldn't read the addons policy."
          onRetry={() => refetch()}
        />
      )
    }
    if (!config) {
      return (
        <div className="space-y-4">
          {ROWS.map((row) => (
            <Skeleton key={row.field} className="h-11 w-full" />
          ))}
        </div>
      )
    }
    return (
      <div className="space-y-4">
        {ROWS.map((row) => {
          const id = `addons-policy-${row.field}`
          return (
            <div
              key={row.field}
              className="flex items-center justify-between gap-4"
            >
              <div className="min-w-0">
                <Label htmlFor={id} className="text-sm font-medium">
                  {row.label}
                </Label>
                <p className="text-xs text-muted-foreground">{row.help}</p>
              </div>
              <div className="shrink-0">
                <Switch
                  id={id}
                  // Every flag defaults to on server-side.
                  checked={config[row.field] ?? true}
                  onCheckedChange={(checked) =>
                    updateMutation.mutate({ [row.field]: checked })
                  }
                  disabled={pendingField === row.field}
                  aria-label={row.label}
                />
              </div>
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <Blocks className="h-5 w-5" />
          Addons
        </CardTitle>
        <CardDescription>
          What Cinna Desktop may let users add beyond the plugins and skills you
          publish.
        </CardDescription>
      </CardHeader>
      <CardContent>{body()}</CardContent>
    </Card>
  )
}
