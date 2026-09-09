import { createFileRoute } from "@tanstack/react-router"
import { useEffect } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import {
  ArrowLeft,
  User,
  Tag,
  GitBranch,
  Globe,
  FolderCode,
  ExternalLink,
  Mail,
  Home,
  Package,
} from "lucide-react"

import { LlmPluginsService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import { addonFormatLabel } from "@/utils/addons"
import { unsupportedReasonSentence } from "@/utils/marketplace"

export const Route = createFileRoute(
  "/_layout/admin/marketplace/plugin/$pluginId"
)({
  component: PluginDetailPage,
})

function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-sm font-medium text-muted-foreground">{children}</div>
}

function PluginDetailPage() {
  const { pluginId } = Route.useParams()

  const { setHeaderContent } = usePageHeader()

  const {
    data: plugin,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["plugin", pluginId],
    queryFn: () => LlmPluginsService.getPlugin({ pluginId }),
    enabled: !!pluginId,
  })

  const { goBack } = useNavigationHistory()

  const handleBack = () => {
    goBack("/admin/marketplaces")
  }

  useEffect(() => {
    if (plugin) {
      setHeaderContent(
        <div className="flex items-center gap-3 min-w-0">
          <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div className="min-w-0">
            <h1 className="text-base font-semibold truncate">{plugin.name}</h1>
            <p className="text-xs text-muted-foreground">Plugin Details</p>
          </div>
        </div>
      )
    }
    return () => setHeaderContent(null)
  }, [plugin, setHeaderContent])

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !plugin) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading plugin details</p>
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

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-6">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Package className="h-5 w-5" />
                  Plugin Information
                </CardTitle>
                <CardDescription>Details about this plugin</CardDescription>
              </div>
              <div className="flex items-center gap-2">
                {plugin.version && (
                  <Badge variant="secondary">v{plugin.version}</Badge>
                )}
                {plugin.category && (
                  <Badge variant="outline">
                    <Tag className="mr-1 h-3 w-3" />
                    {plugin.category}
                  </Badge>
                )}
                <Badge
                  variant="outline"
                  className={isRemote ? undefined : "text-muted-foreground"}
                >
                  {isRemote ? (
                    <>
                      <Globe className="mr-1 h-3 w-3" />
                      Remote
                    </>
                  ) : (
                    <>
                      <FolderCode className="mr-1 h-3 w-3" />
                      Local
                    </>
                  )}
                </Badge>
              </div>
            </div>
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
              <div>
                <Label>Name</Label>
                <p className="text-sm font-medium mt-1">{plugin.name}</p>
              </div>
              <div>
                <Label>Plugin Type</Label>
                <Badge variant="secondary" className="mt-1">
                  {formatLabel}
                </Badge>
              </div>
              <div>
                {/* The label is the category, the value carries the word: the
                    two values are the entries table's cell strings verbatim,
                    but its *column header* would read as "Supported:
                    Supported" over a single row. */}
                <Label>Installation</Label>
                <div className="flex items-center gap-2 mt-1">
                  <span
                    className={`size-2 shrink-0 rounded-full ${
                      supported ? "bg-success" : "bg-destructive"
                    }`}
                  />
                  <span className="text-sm">
                    {supported ? "Supported" : "Not installable"}
                  </span>
                </div>
              </div>
              {plugin.marketplace_name && (
                <div>
                  <Label>Marketplace</Label>
                  <p className="text-sm mt-1">{plugin.marketplace_name}</p>
                </div>
              )}
              <div>
                <Label>Created</Label>
                <p className="text-sm text-muted-foreground mt-1">
                  {new Date(plugin.created_at).toLocaleDateString()}
                </p>
              </div>
            </div>

            {plugin.description && (
              <div className="pt-4 border-t">
                <Label>Description</Label>
                <p className="text-sm mt-1 whitespace-pre-wrap">{plugin.description}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {(plugin.author_name || plugin.author_email || plugin.homepage) && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <User className="h-4 w-4" />
                Author Information
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                {plugin.author_name && (
                  <div>
                    <Label>Author Name</Label>
                    <p className="text-sm mt-1">{plugin.author_name}</p>
                  </div>
                )}
                {plugin.author_email && (
                  <div>
                    <Label>Author Email</Label>
                    <div className="flex items-center gap-1 mt-1">
                      <Mail className="h-3 w-3 text-muted-foreground" />
                      <a
                        href={`mailto:${plugin.author_email}`}
                        className="text-sm text-blue-600 hover:underline"
                      >
                        {plugin.author_email}
                      </a>
                    </div>
                  </div>
                )}
              </div>
              {plugin.homepage && (
                <div>
                  <Label>Homepage</Label>
                  <div className="flex items-center gap-1 mt-1">
                    <Home className="h-3 w-3 text-muted-foreground" />
                    <a
                      href={plugin.homepage}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sm text-blue-600 hover:underline flex items-center gap-1"
                    >
                      {plugin.homepage}
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <GitBranch className="h-4 w-4" />
              Source Information
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label>Source Type</Label>
                <Badge
                  variant="outline"
                  className={isRemote ? "mt-1" : "mt-1 text-muted-foreground"}
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
              </div>
              <div>
                <Label>Branch</Label>
                <div className="flex items-center gap-1 mt-1">
                  <GitBranch className="h-3 w-3" />
                  <span className="text-sm">{plugin.source_branch}</span>
                </div>
              </div>
              {isRemote && plugin.source_url && (
                <div className="col-span-2">
                  <Label>Source URL</Label>
                  <a
                    href={plugin.source_url.replace(/\.git$/, "")}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm text-blue-600 hover:underline flex items-center gap-1 mt-1"
                  >
                    {plugin.source_url}
                    <ExternalLink className="h-3 w-3" />
                  </a>
                </div>
              )}
              {!isRemote && (
                <div className="col-span-2">
                  <Label>Source Path</Label>
                  <code className="text-sm text-muted-foreground bg-muted px-2 py-1 rounded mt-1 block">
                    {plugin.source_path}
                  </code>
                </div>
              )}
              {(plugin.source_commit_hash || plugin.commit_hash) && (
                <div>
                  <Label>Commit Hash</Label>
                  <p className="text-sm font-mono text-muted-foreground mt-1">
                    {(plugin.source_commit_hash || plugin.commit_hash)?.substring(0, 8)}
                  </p>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        {plugin.config && Object.keys(plugin.config).length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Configuration</CardTitle>
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
