import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useState, useEffect } from "react"
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query"
import { ArrowLeft, EllipsisVertical, Edit, Trash } from "lucide-react"

import { KnowledgeSourcesService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { HashTabs } from "@/components/Common/HashTabs"
import { KnowledgeSourceConfigurationTab } from "@/components/KnowledgeSources/KnowledgeSourceConfigurationTab"
import { KnowledgeSourceArticlesTab } from "@/components/KnowledgeSources/KnowledgeSourceArticlesTab"
import { EditSourceModal } from "@/components/KnowledgeSources/EditSourceModal"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/knowledge-source/$sourceId")({
  component: KnowledgeSourceDetailPage,
})

function KnowledgeSourceDetailPage() {
  const { sourceId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [menuOpen, setMenuOpen] = useState(false)
  const [isEditModalOpen, setIsEditModalOpen] = useState(false)

  const deleteMutation = useMutation({
    mutationFn: () => KnowledgeSourcesService.deleteKnowledgeSource({ sourceId }),
    onSuccess: () => {
      showSuccessToast("Knowledge source deleted")
      queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] })
      navigate({ to: "/knowledge-sources" })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete knowledge source")
    },
  })

  const handleDelete = () => {
    if (confirm("Are you sure you want to delete this knowledge source?")) {
      deleteMutation.mutate()
    }
  }

  const {
    data: source,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["knowledge-source", sourceId],
    queryFn: () => KnowledgeSourcesService.getKnowledgeSource({ sourceId }),
    enabled: !!sourceId,
  })

  const handleBack = () => {
    navigate({ to: "/knowledge-sources" })
  }

  // Update header when source loads
  useEffect(() => {
    if (source) {
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <h1 className="text-base font-semibold truncate">{source.name}</h1>
              <p className="text-xs text-muted-foreground">Knowledge Source</p>
            </div>
          </div>
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="shrink-0">
                <EllipsisVertical className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={() => setIsEditModalOpen(true)}>
                <Edit className="mr-2 h-4 w-4" />
                Edit Source
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={handleDelete}
                className="text-destructive focus:text-destructive"
              >
                <Trash className="mr-2 h-4 w-4" />
                Delete Source
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [source, setHeaderContent, menuOpen])

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !source) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading knowledge source details</p>
      </div>
    )
  }

  const tabs = [
    {
      value: "configuration",
      title: "Configuration",
      content: <KnowledgeSourceConfigurationTab source={source} sourceId={sourceId} />,
    },
    {
      value: "articles",
      title: "Articles",
      content: <KnowledgeSourceArticlesTab source={source} sourceId={sourceId} />,
    },
  ]

  return (
    <>
      <div className="p-6 md:p-8 overflow-y-auto">
        <div className="mx-auto max-w-7xl">
          <HashTabs tabs={tabs} defaultTab="configuration" />
        </div>
      </div>

      {isEditModalOpen && (
        <EditSourceModal
          source={source}
          open={isEditModalOpen}
          onOpenChange={setIsEditModalOpen}
          onSuccess={() => {
            queryClient.invalidateQueries({ queryKey: ["knowledge-source", sourceId] })
            setIsEditModalOpen(false)
          }}
        />
      )}
    </>
  )
}
