import type { ColumnDef } from "@tanstack/react-table"
import { Check, Copy } from "lucide-react"

import type { AgentPublic } from "@/client"
import { Button } from "@/components/ui/button"
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard"
import { cn } from "@/lib/utils"
import { AgentActionsMenu } from "./AgentActionsMenu"

function CopyId({ id }: { id: string }) {
  const [copiedText, copy] = useCopyToClipboard()
  const isCopied = copiedText === id

  return (
    <div className="flex items-center gap-1.5 group">
      <span className="font-mono text-xs text-muted-foreground">{id}</span>
      <Button
        variant="ghost"
        size="icon"
        className="size-6 opacity-0 group-hover:opacity-100 transition-opacity"
        onClick={() => copy(id)}
      >
        {isCopied ? (
          <Check className="size-3 text-green-500" />
        ) : (
          <Copy className="size-3" />
        )}
        <span className="sr-only">Copy ID</span>
      </Button>
    </div>
  )
}

export const columns: ColumnDef<AgentPublic>[] = [
  {
    accessorKey: "id",
    header: "ID",
    cell: ({ row }) => <CopyId id={row.original.id} />,
  },
  {
    accessorKey: "name",
    header: "Name",
    cell: ({ row }) => (
      <span className="font-medium">{row.original.name}</span>
    ),
  },
  {
    accessorKey: "workflow_prompt",
    header: "Workflow Prompt",
    cell: ({ row }) => {
      const prompt = row.original.workflow_prompt
      return (
        <span
          className={cn(
            "max-w-xs truncate block text-muted-foreground",
            !prompt && "italic",
          )}
        >
          {prompt || "No workflow prompt"}
        </span>
      )
    },
  },
  {
    accessorKey: "entrypoint_prompt",
    header: "Entrypoint Prompt",
    cell: ({ row }) => {
      const prompt = row.original.entrypoint_prompt
      return (
        <span
          className={cn(
            "max-w-xs truncate block text-muted-foreground",
            !prompt && "italic",
          )}
        >
          {prompt || "No entrypoint prompt"}
        </span>
      )
    },
  },
  {
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <AgentActionsMenu agent={row.original} />
      </div>
    ),
  },
]
