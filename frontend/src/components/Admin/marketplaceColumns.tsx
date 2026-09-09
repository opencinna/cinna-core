import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { AlertCircle, Check, Clock, type LucideIcon, X } from "lucide-react"

import type { LLMPluginMarketplacePublic, MarketplaceStatus } from "@/client"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { marketplaceFormatLabel } from "@/utils/marketplace"
import { MarketplaceActionsMenu } from "./MarketplaceActionsMenu"

/**
 * The sync verdict for one marketplace.
 *
 * Painted from the semantic tokens rather than from `green-100` /
 * `yellow-100` / `red-100`, which is what this file carried until Phase 3
 * touched it: `--success` and `--warning` exist in every skin since
 * 2026-09-07, so a raw palette class on a status element has no
 * established-idiom exemption left (R14a).
 *
 * Solid, not tinted. `--success-foreground` / `--warning-foreground` are
 * defined as *on-solid* partners — near-white in light, near-black in dark —
 * and there is no subtle-foreground token, so the token pair that is
 * guaranteed legible in every skin is the solid one. A 10% tint with the token
 * itself as the text colour measured around 2:1–3:1 on a light ground, under
 * AA for `text-xs`, and it is worse in `hacker-1980`; the `bg-*-100
 * text-*-800` pairing it replaced was compliant, so tinting here would have
 * made the R14a cleanup an accessibility regression. This also matches
 * `components/ui/badge.tsx`, whose variants are all solid.
 */
const STATUS_VARIANTS: Record<
  MarketplaceStatus,
  { icon: LucideIcon; className: string; label: string }
> = {
  connected: {
    icon: Check,
    className: "border-transparent bg-success text-success-foreground",
    label: "Connected",
  },
  pending: {
    icon: Clock,
    className: "border-transparent bg-warning text-warning-foreground",
    label: "Pending",
  },
  error: {
    icon: AlertCircle,
    className: "border-transparent bg-destructive text-white",
    label: "Error",
  },
  disconnected: {
    icon: X,
    className: "border-transparent bg-muted text-muted-foreground",
    label: "Disconnected",
  },
}

// Typed by the generated union rather than `string`, so a fifth marketplace
// status is a compile error here instead of silently rendering "Disconnected".
function StatusBadge({ status }: { status: MarketplaceStatus }) {
  const variant = STATUS_VARIANTS[status] ?? STATUS_VARIANTS.disconnected
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
    header: "Format",
    // What the syncer read the repository as — the one fact that explains why
    // a repository full of skills produced no plugins, and the reason the
    // column is named for the format rather than for the raw `type` value.
    cell: ({ row }) => (
      <Badge variant="secondary">
        {marketplaceFormatLabel(row.original.type)}
      </Badge>
    ),
  },
  {
    accessorKey: "status",
    header: "Status",
    cell: ({ row }) => <StatusBadge status={row.original.status} />,
  },
  {
    accessorKey: "plugin_count",
    header: "Entries",
    cell: ({ row }) => (
      <span
        className={cn(
          "text-sm",
          row.original.plugin_count === 0 && "text-muted-foreground",
        )}
      >
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
