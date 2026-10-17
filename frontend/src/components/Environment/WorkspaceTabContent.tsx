import { TabsContent } from "@/components/ui/tabs"
import { EmptyState } from "./StateComponents"
import { TreeItemRenderer } from "./TreeItemRenderer"
import type { DatabaseTableItem, TreeItem } from "./types"

interface WorkspaceTabContentProps {
  value: string
  data: TreeItem[]
  expandedFolders: Set<string>
  onToggleFolder: (path: string) => void
  onDownload: (fileName: string) => void
  pathPrefix: string
  envId?: string
  databaseTables?: Record<
    string,
    { tables: DatabaseTableItem[]; loading: boolean; error: string | null }
  >
  onFetchDatabaseTables?: (path: string) => void
  isGuest?: boolean
  onPublish?: (path: string, isFolder: boolean) => void
}

export function WorkspaceTabContent({
  value,
  data,
  expandedFolders,
  onToggleFolder,
  onDownload,
  pathPrefix,
  envId,
  databaseTables,
  onFetchDatabaseTables,
  isGuest,
  onPublish,
}: WorkspaceTabContentProps) {
  const firstItem = data[0]
  const hasContent =
    firstItem && firstItem.type === "folder" && firstItem.children.length > 0

  return (
    <TabsContent value={value} className="flex-1 overflow-auto px-4 pb-4">
      {hasContent && firstItem.type === "folder" ? (
        <div className="space-y-1">
          {firstItem.children.map((item: TreeItem, index: number) => (
            <TreeItemRenderer
              key={index}
              item={item}
              level={0}
              expandedFolders={expandedFolders}
              onToggleFolder={onToggleFolder}
              onDownload={onDownload}
              path={pathPrefix}
              envId={envId}
              databaseTables={databaseTables}
              onFetchDatabaseTables={onFetchDatabaseTables}
              isGuest={isGuest}
              onPublish={onPublish}
            />
          ))}
        </div>
      ) : (
        <EmptyState />
      )}
    </TabsContent>
  )
}
