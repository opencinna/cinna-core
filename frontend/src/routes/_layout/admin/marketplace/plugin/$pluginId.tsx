import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import {
  ArrowLeft,
  Braces,
  FolderCode,
  GitBranch,
  Globe,
  Package,
  User,
} from "lucide-react"
import { useEffect } from "react"
import { LlmPluginsService } from "@/client"
import { PluginComponentsCard } from "@/components/Admin/MarketplacePlugin/PluginComponentsCard"
import {
  CopyValueButton,
  Fact,
} from "@/components/Admin/MarketplacePlugin/PluginFacts"
import { PluginSnapshotCard } from "@/components/Admin/MarketplacePlugin/PluginSnapshotCard"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { StatusDot } from "@/components/Common/StatusDot"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
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
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import { usePageHeader } from "@/routes/_layout"
import { addonFormatLabel } from "@/utils/addons"
import {
  snapshotStatusMeta,
  unsupportedReasonSentence,
} from "@/utils/marketplace"

/** How often the page re-reads while its snapshot is still being taken. */
const PENDING_POLL_MS = 5_000

export const Route = createFileRoute(
  "/_layout/admin/marketplace/plugin/$pluginId",
)({
  component: PluginDetailPage,
})

function PluginDetailPage() {
  const { pluginId } = Route.useParams()

  const { setHeaderContent } = usePageHeader()

  const {
    data: plugin,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ["plugin", pluginId],
    queryFn: () => LlmPluginsService.getPlugin({ pluginId }),
    enabled: !!pluginId,
    refetchInterval: (query) =>
      query.state.data?.snapshot_status === "pending" ? PENDING_POLL_MS : false,
  })

  const { goBack } = useNavigationHistory()

  const handleBack = () => {
    goBack("/admin/marketplaces")
  }

  useEffect(() => {
    if (plugin) {
      setHeaderContent(
        <div className="flex items-center gap-3 min-w-0">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleBack}
                className="shrink-0"
                aria-label="Back"
              >
                <ArrowLeft className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="text-xs">
              Back
            </TooltipContent>
          </Tooltip>
          <div className="min-w-0">
            <h1 className="text-base font-semibold truncate">{plugin.name}</h1>
            <p className="text-xs text-muted-foreground">Plugin Details</p>
          </div>
        </div>,
      )
    }
    return () => setHeaderContent(null)
  }, [plugin, setHeaderContent])

  if (isLoading) {
    return (
      <div className="p-6 md:p-8">
        <div className="mx-auto max-w-4xl space-y-6">
          <Skeleton className="h-64 w-full rounded-xl" />
          <Skeleton className="h-40 w-full rounded-xl" />
          <Skeleton className="h-56 w-full rounded-xl" />
        </div>
      </div>
    )
  }

  if (error || !plugin) {
    return (
      <div className="p-6 md:p-8">
        <div className="mx-auto max-w-4xl">
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load this plugin"
            onRetry={() => refetch()}
          />
        </div>
      </div>
    )
  }

  const isRemote = plugin.source_type === "url"
  // Optional on the wire (server-side default), so an entry synced before the
  // flag existed reads as installable rather than as broken — the same test
  // the entries table makes.
  const supported = plugin.supported !== false
  // The entry's own format in words. Degrades to the stored code rather than
  // to nothing: a row written by a newer server is still a real row, and an
  // admin who can read its `plugin_type` can act on it.
  const formatLabel = addonFormatLabel(plugin.plugin_type) ?? plugin.plugin_type
  // No snapshot state (a format without one, or a row not re-synced since the
  // columns existed) renders neither card; `no_components` is said by the
  // Snapshot card's Alert alone.
  const hasSnapshot = snapshotStatusMeta(plugin.snapshot_status) !== null
  const showComponents =
    (hasSnapshot || !!plugin.components) &&
    plugin.snapshot_reason !== "no_components"
  const commitHash = plugin.source_commit_hash || plugin.commit_hash || null

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex min-w-0 items-center gap-2">
              <Package className="h-5 w-5" />
              Plugin Information
            </CardTitle>
            <CardDescription>
              What this marketplace entry is and whether the cloud can install
              it.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Why it cannot be installed — the question the entries table's
                Supported column raises and sends the admin here to answer.
                The server's own sentence, keyed off the stable code, so a
                refusal reads the same here, in the entries table and in the
                409 an install answers with. */}
            {!supported && (
              <Alert>
                <AlertDescription>
                  {unsupportedReasonSentence(plugin.unsupported_reason)}
                </AlertDescription>
              </Alert>
            )}
            <div className="grid grid-cols-2 gap-4">
              <Fact label="Name">
                <span className="font-medium">{plugin.name}</span>
              </Fact>
              <Fact label="Plugin Type">
                <Badge variant="secondary">{formatLabel}</Badge>
              </Fact>
              {/* The entries list's column name, so the two read alike; the
                  snapshot (desktop delivery) is its own card below. */}
              <Fact label="Cloud install">
                <StatusDot
                  tone={supported ? "on" : "error"}
                  label={supported ? "Supported" : "Not installable"}
                />
              </Fact>
              {plugin.version && <Fact label="Version">{plugin.version}</Fact>}
              {plugin.category && (
                <Fact label="Category">{plugin.category}</Fact>
              )}
              {plugin.marketplace_name && (
                <Fact label="Marketplace">{plugin.marketplace_name}</Fact>
              )}
              <Fact label="Created">
                <RelativeTime
                  timestamp={plugin.created_at}
                  className="text-muted-foreground"
                />
              </Fact>
            </div>

            {plugin.description && (
              <Fact label="Description" className="border-t pt-4">
                <p className="whitespace-pre-wrap break-words">
                  {plugin.description}
                </p>
              </Fact>
            )}
          </CardContent>
        </Card>

        {hasSnapshot && <PluginSnapshotCard plugin={plugin} />}
        {showComponents && <PluginComponentsCard plugin={plugin} />}

        {(plugin.author_name || plugin.author_email || plugin.homepage) && (
          <Card>
            <CardHeader>
              <CardTitle className="flex min-w-0 items-center gap-2">
                <User className="h-5 w-5" />
                Author
              </CardTitle>
              <CardDescription>
                Who publishes this entry, as its manifest says.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-4">
                {plugin.author_name && (
                  <Fact label="Name">{plugin.author_name}</Fact>
                )}
                {plugin.author_email && (
                  <Fact label="Email">
                    <a
                      href={`mailto:${plugin.author_email}`}
                      className="block truncate text-primary hover:underline"
                    >
                      {plugin.author_email}
                    </a>
                  </Fact>
                )}
                {plugin.homepage && (
                  <Fact label="Homepage" className="col-span-2">
                    <a
                      href={plugin.homepage}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="block truncate text-primary hover:underline"
                    >
                      {plugin.homepage}
                    </a>
                  </Fact>
                )}
              </div>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle className="flex min-w-0 items-center gap-2">
              <GitBranch className="h-5 w-5" />
              Source
            </CardTitle>
            <CardDescription>
              Where the marketplace says this entry is fetched from.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-4">
              <Fact label="Source Type">
                <Badge
                  variant="outline"
                  className={isRemote ? undefined : "text-muted-foreground"}
                >
                  {isRemote ? (
                    <>
                      <Globe className="mr-1 h-3 w-3" />
                      Remote URL
                    </>
                  ) : (
                    <>
                      <FolderCode className="mr-1 h-3 w-3" />
                      Local Path
                    </>
                  )}
                </Badge>
              </Fact>
              {plugin.source_branch && (
                <Fact label="Branch">{plugin.source_branch}</Fact>
              )}
              {isRemote && plugin.source_url && (
                <Fact label="Source URL" className="col-span-2">
                  <a
                    href={plugin.source_url.replace(/\.git$/, "")}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block truncate text-primary hover:underline"
                  >
                    {plugin.source_url}
                  </a>
                </Fact>
              )}
              {!isRemote && (
                <Fact label="Source Path" className="col-span-2">
                  <code className="block break-all rounded bg-muted px-2 py-1 text-muted-foreground">
                    {plugin.source_path}
                  </code>
                </Fact>
              )}
              {commitHash && (
                <Fact label="Commit">
                  <CopyValueButton
                    value={commitHash}
                    display={commitHash.substring(0, 8)}
                    noun="Commit hash"
                  />
                </Fact>
              )}
            </div>
          </CardContent>
        </Card>

        {plugin.config && Object.keys(plugin.config).length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle className="flex min-w-0 items-center gap-2">
                <Braces className="h-5 w-5" />
                Configuration
              </CardTitle>
              <CardDescription>
                The entry as the marketplace manifest declares it.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="text-sm bg-muted p-4 rounded overflow-auto">
                {JSON.stringify(plugin.config, null, 2)}
              </pre>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  )
}
