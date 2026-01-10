import { TabsContent } from "@/components/ui/tabs"
import { TreeItemRenderer } from "./TreeItemRenderer"
import { EmptyState } from "./StateComponents"
import type { TreeItem, DatabaseTableItem } from "./types"

interface WorkspaceTabContentProps {
  value: string
  data: TreeItem[]
  expandedFolders: Set<string>
  onToggleFolder: (path: string) => void
  onDownload: (fileName: string) => void
  pathPrefix: string
  envId?: string
  databaseTables?: Record<string, { tables: DatabaseTableItem[], loading: boolean, error: string | null }>
  onFetchDatabaseTables?: (path: string) => void
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
}: WorkspaceTabContentProps) {
  const hasContent = data[0]?.children && data[0].children.length > 0

  return (
    <TabsContent value={value} className="flex-1 overflow-auto px-4 pb-4">
      {hasContent ? (
        <div className="space-y-1">
          {data[0].children.map((item, index) => (
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
            />
          ))}
        </div>
      ) : (
        <EmptyState />
      )}
    </TabsContent>
  )
}
