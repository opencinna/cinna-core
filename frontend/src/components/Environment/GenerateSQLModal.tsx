import { useState } from "react"
import { useMutation } from "@tanstack/react-query"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Sparkles, Loader2, AlertCircle } from "lucide-react"
import { WorkspaceService } from "@/client"

interface GenerateSQLModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  envId: string
  dbPath: string
  currentQuery: string
  onQueryGenerated: (sql: string) => void
}

export function GenerateSQLModal({
  open,
  onOpenChange,
  envId,
  dbPath,
  currentQuery,
  onQueryGenerated,
}: GenerateSQLModalProps) {
  const [userRequest, setUserRequest] = useState("")

  const generateMutation = useMutation({
    mutationFn: async () => {
      const response = await WorkspaceService.generateSqlQuery({
        envId,
        requestBody: {
          path: dbPath,
          user_request: userRequest,
          current_query: currentQuery || undefined,
        },
      })
      return response as { success: boolean; sql?: string; error?: string }
    },
    onSuccess: (data) => {
      if (data.success && data.sql) {
        onQueryGenerated(data.sql)
        setUserRequest("")
        onOpenChange(false)
      }
    },
  })

  const handleGenerate = () => {
    if (userRequest.trim()) {
      generateMutation.mutate()
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleGenerate()
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-purple-500" />
            Generate SQL Query
          </DialogTitle>
          <DialogDescription>
            Describe what data you want to retrieve or modify, and AI will
            generate the SQL query for you.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 pt-2">
          <Textarea
            value={userRequest}
            onChange={(e) => setUserRequest(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="e.g., Select all records created in May 2025, Show top 10 users by order count, Count items per category..."
            className="min-h-[100px] resize-none"
            autoFocus
          />
          <p className="text-xs text-muted-foreground">
            Press Ctrl+Enter to generate
          </p>

          {/* Error display */}
          {generateMutation.isError && (
            <div className="flex items-start gap-2 p-3 rounded-md bg-destructive/10 text-destructive text-sm">
              <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
              <span>
                {(generateMutation.error as Error)?.message ||
                  "Failed to generate query"}
              </span>
            </div>
          )}

          {/* API returned error (not exception) */}
          {generateMutation.data && !generateMutation.data.success && (
            <div className="flex items-start gap-2 p-3 rounded-md bg-amber-500/10 text-amber-700 dark:text-amber-400 text-sm">
              <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
              <span>{generateMutation.data.error}</span>
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={generateMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              onClick={handleGenerate}
              disabled={!userRequest.trim() || generateMutation.isPending}
            >
              {generateMutation.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Generating...
                </>
              ) : (
                <>
                  <Sparkles className="h-4 w-4 mr-2" />
                  Generate
                </>
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
