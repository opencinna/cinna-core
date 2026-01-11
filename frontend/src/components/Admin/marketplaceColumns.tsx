import type { ColumnDef } from "@tanstack/react-table"
import { Link } from "@tanstack/react-router"
import { Check, X, Clock, AlertCircle } from "lucide-react"

import type { LLMPluginMarketplacePublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { MarketplaceActionsMenu } from "./MarketplaceActionsMenu"

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

export const marketplaceColumns: ColumnDef<LLMPluginMarketplacePublic>[] = [
  {
    accessorKey: "name",
    header: "Name",
    cell: ({ row }) => {
      const marketplace = row.original
      return (
        <Link
          to="/admin/marketplace/$marketplaceId"
          params={{ marketplaceId: marketplace.id }}
          className="font-medium text-primary hover:underline"
        >
          {marketplace.name}
        </Link>
      )
    },
  },
  {
    accessorKey: "url",
    header: "Repository URL",
    cell: ({ row }) => (
      <span className="text-sm text-muted-foreground font-mono truncate max-w-[300px] block">
        {row.original.url}
      </span>
    ),
  },
  {
    accessorKey: "type",
    header: "Type",
    cell: ({ row }) => (
      <Badge variant="secondary">{row.original.type}</Badge>
    ),
  },
  {
    accessorKey: "status",
    header: "Status",
    cell: ({ row }) => <StatusBadge status={row.original.status} />,
  },
  {
    accessorKey: "plugin_count",
    header: "Plugins",
    cell: ({ row }) => (
      <span className={cn(
        "text-sm",
        row.original.plugin_count === 0 && "text-muted-foreground"
      )}>
        {row.original.plugin_count ?? 0}
      </span>
    ),
  },
  {
    accessorKey: "public_discovery",
    header: "Visibility",
    cell: ({ row }) => (
      <Badge variant={row.original.public_discovery ? "default" : "outline"}>
        {row.original.public_discovery ? "Public" : "Private"}
      </Badge>
    ),
  },
  {
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <MarketplaceActionsMenu marketplace={row.original} />
      </div>
    ),
  },
]
