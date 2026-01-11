import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Puzzle, AlertCircle, User, Tag, FolderCode, Globe } from "lucide-react"

import type { LLMPluginMarketplacePublic, LLMPluginMarketplacePluginPublic } from "@/client"
import { LlmPluginsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"

interface MarketplacePluginsTabProps {
  marketplace: LLMPluginMarketplacePublic
  marketplaceId: string
}

function truncateDescription(description: string | null, maxLength: number = 80): string {
  if (!description) return "No description"
  if (description.length <= maxLength) return description
  return description.substring(0, maxLength) + "..."
}

function PluginRow({ plugin }: { plugin: LLMPluginMarketplacePluginPublic }) {
  const isRemote = plugin.source_type === "url"

  return (
    <TableRow>
      <TableCell>
        <Link
          to="/admin/marketplace/plugin/$pluginId"
          params={{ pluginId: plugin.id }}
          className="font-medium text-primary hover:underline"
        >
          {plugin.name}
        </Link>
        <div className="flex items-center gap-1 mt-1">
          {plugin.version && (
            <Badge variant="secondary" className="text-xs">
              v{plugin.version}
            </Badge>
          )}
          {plugin.category && (
            <Badge variant="outline" className="text-xs">
              <Tag className="mr-1 h-3 w-3" />
              {plugin.category}
            </Badge>
          )}
        </div>
      </TableCell>
      <TableCell>
        <span className="text-sm text-muted-foreground">
          {truncateDescription(plugin.description)}
        </span>
      </TableCell>
      <TableCell>
        {plugin.author_name && (
          <div className="flex items-center gap-1 text-sm text-muted-foreground">
            <User className="h-3 w-3" />
            <span>{plugin.author_name}</span>
          </div>
        )}
      </TableCell>
      <TableCell>
        {isRemote ? (
          <Badge variant="outline" className="text-xs bg-blue-50 text-blue-700 border-blue-200">
            <Globe className="mr-1 h-3 w-3" />
            Remote
          </Badge>
        ) : (
          <Badge variant="outline" className="text-xs bg-gray-50 text-gray-700 border-gray-200">
            <FolderCode className="mr-1 h-3 w-3" />
            Local
          </Badge>
        )}
      </TableCell>
    </TableRow>
  )
}

export function MarketplacePluginsTab({
  marketplace,
  marketplaceId,
}: MarketplacePluginsTabProps) {
  // We'll filter plugins by marketplace_id in the discover endpoint
  // Since discover returns all plugins, we filter client-side for the marketplace
  const { data: pluginsResponse, isLoading: isLoadingPlugins } = useQuery({
    queryKey: ["marketplace-plugins", marketplaceId],
    queryFn: async () => {
      const response = await LlmPluginsService.discoverPlugins({})
      // Filter plugins for this marketplace
      const filtered = response.data.filter(
        (plugin) => plugin.marketplace_id === marketplaceId
      )
      return { data: filtered, count: filtered.length }
    },
    enabled: !!marketplace && marketplace.status === "connected",
  })

  const plugins = pluginsResponse?.data || []

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Puzzle className="h-5 w-5" />
              Plugins
            </CardTitle>
            <CardDescription>
              Plugins available in this marketplace ({marketplace.plugin_count ?? 0} total)
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {marketplace.status !== "connected" ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <AlertCircle className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold">Marketplace not connected</h3>
            <p className="text-sm text-muted-foreground mt-2">
              Sync the marketplace in the Configuration tab to load plugins
            </p>
          </div>
        ) : isLoadingPlugins ? (
          <div className="space-y-2">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : plugins.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <Puzzle className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold">No plugins found</h3>
            <p className="text-sm text-muted-foreground mt-2">
              Click "Sync Marketplace" in the Configuration tab to fetch plugins
            </p>
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Author</TableHead>
                <TableHead>Type</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {plugins.map((plugin) => (
                <PluginRow key={plugin.id} plugin={plugin} />
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
