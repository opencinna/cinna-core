import { useState, useEffect } from "react"
import { Bot, Sparkles, ArrowRight } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import { getColorPreset } from "@/utils/colorPresets"
import type { AgenticTeamConnectionPublic } from "@/client"

interface ConnectionEditDialogProps {
  open: boolean
  onClose: () => void
  connection: AgenticTeamConnectionPublic | null
  onSave: (connectionId: string, prompt: string, enabled: boolean) => void
  onGenerate: (connectionId: string) => void
  isPending: boolean
  isGenerating: boolean
  generatedPrompt: string | null
}

export function ConnectionEditDialog({
  open,
  onClose,
  connection,
  onSave,
  onGenerate,
  isPending,
  isGenerating,
  generatedPrompt,
}: ConnectionEditDialogProps) {
  const [prompt, setPrompt] = useState("")
  const [enabled, setEnabled] = useState(true)

  // Track which connection this generated prompt belongs to
  const [appliedGeneratedPrompt, setAppliedGeneratedPrompt] = useState<string | null>(null)

  useEffect(() => {
    if (open && connection) {
      setPrompt(connection.connection_prompt)
      setEnabled(connection.enabled)
      setAppliedGeneratedPrompt(null)
    }
  }, [open, connection])

  // Apply generated prompt when it arrives (only if dialog is still open for the same connection)
  useEffect(() => {
    if (generatedPrompt !== null && generatedPrompt !== appliedGeneratedPrompt && open) {
      setPrompt(generatedPrompt)
      setAppliedGeneratedPrompt(generatedPrompt)
    }
  }, [generatedPrompt, appliedGeneratedPrompt, open])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!connection) return
    onSave(connection.id, prompt, enabled)
  }

  const sourceColor = getColorPreset(connection?.source_node_color_preset)
  const targetColor = getColorPreset(connection?.target_node_color_preset)

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[500px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Edit Connection</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            {connection && (
              <div className="flex items-center gap-2 flex-wrap">
                <span
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm font-medium ${sourceColor.badgeBg} ${sourceColor.badgeText}`}
                >
                  <Bot className="h-3.5 w-3.5" />
                  {connection.source_node_name}
                </span>
                <ArrowRight className="h-4 w-4 text-muted-foreground shrink-0" />
                <span
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm font-medium ${targetColor.badgeBg} ${targetColor.badgeText}`}
                >
                  <Bot className="h-3.5 w-3.5" />
                  {connection.target_node_name}
                </span>
              </div>
            )}
            <div className="grid gap-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="conn-prompt">Handover Prompt</Label>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => connection && onGenerate(connection.id)}
                  disabled={isGenerating || !connection}
                >
                  <Sparkles className="h-4 w-4 mr-1" />
                  {isGenerating ? "Generating..." : "Generate"}
                </Button>
              </div>
              <Textarea
                id="conn-prompt"
                placeholder="Describe when and how to hand over work from the source agent to the target agent. Include trigger conditions, context to pass, and expected output format."
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={5}
                maxLength={2000}
                className="resize-none"
              />
              <div className="text-xs text-muted-foreground text-right">
                {prompt.length}/2000
              </div>
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="conn-enabled">Connection enabled</Label>
              <Switch
                id="conn-enabled"
                checked={enabled}
                onCheckedChange={setEnabled}
              />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
