import { useId } from "react"

import { AgentComponentView } from "@/components/Dashboard/AgentComponents/AgentComponentView"
import { readView } from "@/components/Dashboard/AgentComponents/componentSpec"
import { useOpenLinkConfirm } from "@/components/Dashboard/AgentComponents/OpenLinkConfirm"
import { useAgentComponent } from "@/components/Dashboard/AgentComponents/useAgentComponent"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { usePreviewController } from "./usePreviewController"

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  agent: { id: string; name: string }
  name: string
  skill: string | null
  title: string
  /** The component is addable (resolved, no errors). */
  canAdd: boolean
  /** Close the preview, then open Add to dashboard (chained, not nested). */
  onAdd: () => void
}

/**
 * Preview of a referenced component (UI spec S6a): the shared view in
 * `preview` mode — params, body, Updated and Refresh — over the agent-scoped
 * routes. No Ask, no focus, no highlights.
 */
export default function ComponentPreviewDialog({
  open,
  onOpenChange,
  agent,
  name,
  skill,
  title,
  canAdd,
  onAdd,
}: Props) {
  const tileKey = `preview-${useId()}`
  const componentQuery = useAgentComponent(agent.id, name, skill)
  const component = componentQuery.data
  const controller = usePreviewController({
    agentId: agent.id,
    name,
    skill,
    view: readView(component),
  })
  const { confirmLink, dialog } = useOpenLinkConfirm(title)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-3xl [&>*]:min-w-0"
        onOpenAutoFocus={(event) => {
          // A params control with a tooltip must not open it on mount.
          event.preventDefault()
          ;(event.currentTarget as HTMLElement | null)?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="break-words">{title}</DialogTitle>
          <DialogDescription className="break-words">
            {[agent.name, skill ? `from skill ${skill}` : null]
              .filter(Boolean)
              .join(" · ")}
          </DialogDescription>
        </DialogHeader>
        <div className="h-[60vh] overflow-hidden rounded-md border">
          <AgentComponentView
            mode="preview"
            tileKey={tileKey}
            agent={agent}
            component={component}
            componentError={
              componentQuery.isError ? componentQuery.error : null
            }
            controller={controller}
            title={title}
            frameQueryKey={[
              "agentComponentPreviewFrame",
              agent.id,
              name,
              skill,
            ]}
            onRetryComponent={() => void componentQuery.refetch()}
            confirmLink={confirmLink}
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          {canAdd && <Button onClick={onAdd}>Add to dashboard…</Button>}
        </DialogFooter>
        {dialog}
      </DialogContent>
    </Dialog>
  )
}
