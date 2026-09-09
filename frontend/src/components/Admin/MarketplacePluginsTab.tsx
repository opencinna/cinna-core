import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { AlertCircle, FolderCode, Globe, Puzzle, Tag, User } from "lucide-react"

import type {
  LLMPluginMarketplacePluginPublic,
  LLMPluginMarketplacePublic,
} from "@/client"
import { LlmPluginsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { unsupportedReasonSentence } from "@/utils/marketplace"

/**
 * How many discover rows this table pulls before filtering to one marketplace.
 *
 * Large rather than paginated because the endpoint cannot filter by
 * marketplace: any page smaller than the instance's total entry count silently
 * drops rows from this table.
 */
const DISCOVER_PAGE = 500

interface MarketplacePluginsTabProps {
  marketplace: LLMPluginMarketplacePublic
  marketplaceId: string
}

function truncateDescription(
  description: string | null,
  maxLength: number = 80,
): string {
  if (!description) return "No description"
  if (description.length <= maxLength) return description
  return description.substring(0, maxLength) + "..."
}

/**
 * How many skills this entry ships, when the syncer could tell.
 *
 * Two shapes, because two parsers write them: a `skills`-format entry *is* one
 * skill and carries a `skill_summary`; a **local** Codex entry has its skill
 * folders listed into `config["skills"]` at sync time. Absence means "not
 * known", never "ships no skills" — a Codex `url` / `git-subdir` entry is
 * never cloned at sync, so it has no count either, and a `0` there would be an
 * assertion the syncer never made.
 *
 * Two guards, both against `config` being repository-authored JSON:
 *
 * - An unsupported entry reports nothing. A `skills`-format entry refused as
 *   `no_skill_md` still gets a `config["skill"]` block written for it, so
 *   without this the row would read "Not installable" beside "Skills: 1" — a
 *   skill it has just said has nothing to install.
 * - Only a `codex` entry's `skills` key is read. For a `claude` entry `config`
 *   is the raw `marketplace.json` entry, stored verbatim, so any `skills`
 *   array the catalog's author happened to write would otherwise be rendered
 *   as a syncer-verified count.
 */
function skillCount(plugin: LLMPluginMarketplacePluginPublic): number | null {
  if (plugin.supported === false) return null
  if (plugin.skill_summary) return 1
  if (plugin.plugin_type !== "codex") return null
  const config = plugin.config
  const folders = config && typeof config === "object" ? config.skills : null
  if (Array.isArray(folders)) return folders.length
  return null
}

/**
 * Can this platform install the entry, and if not, why not.
 *
 * A tone-dot plus a label rather than a `Badge`: "supported" and "why not" are
 * two unequal facts, and two chips in one cell would flatten them (§5). The
 * reason is the server's own sentence, keyed off the stable code, so a refusal
 * reads the same here as it does in the Add addon dialog and in the 409 an
 * install answers with.
 */
function SupportedCell({
  plugin,
}: {
  plugin: LLMPluginMarketplacePluginPublic
}) {
  // Optional on the wire (it has a server-side default), so an entry synced
  // before this column existed reads as supported rather than as broken.
  const supported = plugin.supported !== false

  if (supported) {
    return (
      <div className="flex items-center gap-2">
        <span className="size-2 shrink-0 rounded-full bg-success" />
        <span className="text-sm">Supported</span>
      </div>
    )
  }

  const reason = unsupportedReasonSentence(plugin.unsupported_reason)

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="flex w-fit items-center gap-2">
          <span className="size-2 shrink-0 rounded-full bg-destructive" />
          {/* The dotted underline is the house's "there is more here on
              hover" affordance (`Channels/ChannelRow`); the sentence is also
              `sr-only`, so a reader that never gets the tooltip still gets the
              reason. */}
          <span className="cursor-default text-sm underline decoration-dotted underline-offset-2">
            Not installable
          </span>
          <span className="sr-only">{reason}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-xs">
        {reason}
      </TooltipContent>
    </Tooltip>
  )
}

function PluginRow({ plugin }: { plugin: LLMPluginMarketplacePluginPublic }) {
  const isRemote = plugin.source_type === "url"
  const skills = skillCount(plugin)

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
        <SupportedCell plugin={plugin} />
      </TableCell>
      <TableCell>
        <span
          className={
            skills === null ? "text-sm text-muted-foreground" : "text-sm"
          }
        >
          {skills ?? "—"}
        </span>
      </TableCell>
      <TableCell>
        {isRemote ? (
          <Badge variant="outline" className="text-xs">
            <Globe className="mr-1 h-3 w-3" />
            Remote
          </Badge>
        ) : (
          <Badge variant="outline" className="text-xs text-muted-foreground">
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
  // `/discover` has no `marketplace_id` filter, so this reads a page of the
  // union of the user's private marketplaces and every public one and keeps
  // the rows belonging to this one. The explicit limit is load-bearing: the
  // route defaults to **30** (`llm_plugins.py:227`) and orders unsupported
  // entries *last*, so the default would truncate away precisely the rows the
  // Supported column exists to show — and could render "No plugins or skills
  // found" for a perfectly synced marketplace sitting behind a busier one.
  // The real fix is a `marketplace_id` filter on the endpoint.
  const {
    data: pluginsResponse,
    isLoading: isLoadingPlugins,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["marketplace-plugins", marketplaceId, DISCOVER_PAGE],
    queryFn: async () => {
      const response = await LlmPluginsService.discoverPlugins({
        limit: DISCOVER_PAGE,
      })
      const filtered = response.data.filter(
        (plugin) => plugin.marketplace_id === marketplaceId,
      )
      // `count` is the unfiltered total across every marketplace the page was
      // drawn from, so a full page means rows may exist that this table never
      // saw — said out loud rather than left as a silently short list.
      return {
        data: filtered,
        count: filtered.length,
        truncated: response.data.length >= DISCOVER_PAGE,
      }
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
              Plugins and skills
            </CardTitle>
            <CardDescription>
              Plugins and skills published by this marketplace (
              {marketplace.plugin_count ?? 0} total)
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
              Sync the marketplace in the Configuration tab to load entries
            </p>
          </div>
        ) : isLoadingPlugins ? (
          <div className="space-y-2">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : /* A failed read is not an empty marketplace. Without this branch an
              admin who hits a 500 here reads "No entries found" and goes and
              re-syncs a repository that was never the problem. Gated on
              `!pluginsResponse` so a failed background refetch keeps the rows
              that are already on screen. */
        isError && !pluginsResponse ? (
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load this marketplace's entries"
            onRetry={() => refetch()}
          />
        ) : plugins.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <Puzzle className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold">
              No plugins or skills found
            </h3>
            <p className="text-sm text-muted-foreground mt-2">
              Click "Sync Marketplace" in the Configuration tab to fetch them
            </p>
          </div>
        ) : (
          <>
            {pluginsResponse?.truncated && (
              <p className="mb-3 text-xs text-muted-foreground">
                Showing the first {DISCOVER_PAGE} entries across all
                marketplaces — this list may be incomplete.
              </p>
            )}
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Author</TableHead>
                  <TableHead>Supported</TableHead>
                  <TableHead>Skills</TableHead>
                  <TableHead>Source</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {plugins.map((plugin) => (
                  <PluginRow key={plugin.id} plugin={plugin} />
                ))}
              </TableBody>
            </Table>
          </>
        )}
      </CardContent>
    </Card>
  )
}
