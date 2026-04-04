import { createFileRoute, Link } from "@tanstack/react-router"
import { useState, useEffect } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, BookOpen, Check, X, Clock, AlertCircle, Globe } from "lucide-react"

import { KnowledgeSourcesService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import PendingItems from "@/components/Pending/PendingItems"
import { AddSourceModal } from "@/components/KnowledgeSources/AddSourceModal"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/knowledge-sources")({
  component: KnowledgeSourcesPage,
  head: () => ({
    meta: [
      {
        title: "Knowledge Sources - Workflow Runner",
      },
    ],
  }),
})

function StatusBadge({ status }: { status: string }) {
  const variants: Record<string, { icon: any; className: string; label: string }> = {
    connected: {
      icon: Check,
      className: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-300",
      label: "Connected",
    },
    pending: {
      icon: Clock,
      className: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-300",
      label: "Pending",
    },
    error: {
      icon: AlertCircle,
      className: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300",
      label: "Error",
    },
    disconnected: {
      icon: X,
      className: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-300",
      label: "Disconnected",
    },
  }

  const variant = variants[status] || variants.disconnected
  const Icon = variant.icon

  return (
    <Badge className={variant.className} variant="outline">
      <Icon className="mr-1 h-3 w-3" />
      {variant.label}
    </Badge>
  )
}

function MyKnowledgeSourcesList() {
  const { data: sources, isLoading, error } = useQuery({
    queryKey: ["knowledge-sources"],
    queryFn: () => KnowledgeSourcesService.listKnowledgeSources(),
  })

  if (isLoading) {
    return <PendingItems />
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading knowledge sources: {(error as Error).message}
        </p>
      </div>
    )
  }

  if (!sources || sources.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12">
        <div className="rounded-full bg-muted p-4 mb-4">
          <BookOpen className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold">No knowledge sources</h3>
        <p className="text-muted-foreground">Add your first knowledge source to help your agents build integrations</p>
      </div>
    )
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Visibility</TableHead>
          <TableHead>Articles</TableHead>
          <TableHead>Last Sync</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sources.map((source) => (
          <TableRow
            key={source.id}
            className={!source.is_enabled ? "opacity-60" : ""}
          >
            <TableCell className="font-medium">
              <Link
                to="/knowledge-source/$sourceId"
                params={{ sourceId: source.id }}
                className="text-primary hover:underline"
              >
                {source.name}
              </Link>
            </TableCell>
            <TableCell>
              <StatusBadge status={source.status || "disconnected"} />
            </TableCell>
            <TableCell>
              {source.public_discovery ? (
                <Badge variant="outline" className="bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                  <Globe className="mr-1 h-3 w-3" />
                  Public
                </Badge>
              ) : (
                <span className="text-xs text-muted-foreground">Private</span>
              )}
            </TableCell>
            <TableCell>
              <Badge variant="secondary">{source.article_count}</Badge>
            </TableCell>
            <TableCell className="text-xs text-muted-foreground">
              {source.last_sync_at
                ? new Date(source.last_sync_at).toLocaleDateString()
                : "Never"}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function DiscoverableSourcesList() {
  const { data: sources, isLoading, error } = useQuery({
    queryKey: ["discoverable-sources"],
    queryFn: () => KnowledgeSourcesService.listDiscoverableSources(),
  })

  if (isLoading) {
    return <PendingItems />
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-6">
        <p className="text-destructive text-sm">
          Error loading discoverable sources: {(error as Error).message}
        </p>
      </div>
    )
  }

  if (!sources || sources.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-8">
        <div className="rounded-full bg-muted p-3 mb-3">
          <Globe className="h-6 w-6 text-muted-foreground" />
        </div>
        <p className="text-sm text-muted-foreground">No public sources from other admins</p>
      </div>
    )
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Owner</TableHead>
          <TableHead>Articles</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sources.map((source) => (
          <TableRow key={source.id}>
            <TableCell className="font-medium">
              <div>
                {source.name}
                {source.description && (
                  <p className="text-xs text-muted-foreground truncate max-w-xs">
                    {source.description}
                  </p>
                )}
              </div>
            </TableCell>
            <TableCell className="text-sm text-muted-foreground">
              {source.owner_username || "—"}
            </TableCell>
            <TableCell>
              <Badge variant="secondary">{source.article_count}</Badge>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function KnowledgeSourcesPage() {
  const { setHeaderContent } = usePageHeader()
  const [isAddModalOpen, setIsAddModalOpen] = useState(false)
  const queryClient = useQueryClient()

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Knowledge Sources</h1>
          <p className="text-xs text-muted-foreground">Manage Git-based knowledge repositories for agents</p>
        </div>
        <Button onClick={() => setIsAddModalOpen(true)}>
          <Plus className="mr-2 h-4 w-4" />
          Add Source
        </Button>
      </>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-8">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <BookOpen className="h-5 w-5" />
              My Knowledge Sources
            </CardTitle>
            <CardDescription>
              Knowledge sources you manage. Public sources are available to all users.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <MyKnowledgeSourcesList />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Globe className="h-5 w-5" />
              Discoverable Sources
            </CardTitle>
            <CardDescription>
              Public knowledge sources from other admins, automatically available to all users.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DiscoverableSourcesList />
          </CardContent>
        </Card>
      </div>

      <AddSourceModal
        open={isAddModalOpen}
        onOpenChange={setIsAddModalOpen}
        onSuccess={() => {
          queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] })
          setIsAddModalOpen(false)
        }}
      />
    </div>
  )
}
