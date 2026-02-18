import { useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { AgentSharesService } from "@/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

import { AddShareForm } from "./AddShareForm"
import { ShareList } from "./ShareList"
import { ClonesList } from "./ClonesList"

interface ShareManagementDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  agentId: string
  agentName: string
}

export function ShareManagementDialog({
  open,
  onOpenChange,
  agentId,
  agentName,
}: ShareManagementDialogProps) {
  const [activeTab, setActiveTab] = useState("shares")

  // Fetch shares for this agent
  const { data: shares, refetch: refetchShares } = useQuery({
    queryKey: ["agentShares", agentId],
    queryFn: () => AgentSharesService.getAgentShares({ agentId }),
    enabled: open,
  })

  // Fetch clones for this agent
  const { data: clones, refetch: refetchClones } = useQuery({
    queryKey: ["agentClones", agentId],
    queryFn: () => AgentSharesService.getAgentClones({ agentId }),
    enabled: open,
  })

  const handleShareCreated = () => {
    refetchShares()
  }

  const handleShareRevoked = () => {
    refetchShares()
    refetchClones()
  }

  const handleUpdatesPushed = () => {
    refetchClones()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>Share Management: {agentName}</DialogTitle>
        </DialogHeader>

        <Tabs value={activeTab} onValueChange={setActiveTab} className="flex-1 overflow-hidden flex flex-col">
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger value="shares">
              Shares ({shares?.count || 0})
            </TabsTrigger>
            <TabsTrigger value="add">
              Add Share
            </TabsTrigger>
            <TabsTrigger value="updates">
              Push Updates
            </TabsTrigger>
          </TabsList>

          <div className="mt-4 overflow-auto flex-1">
            <TabsContent value="shares" className="m-0">
              <ShareList
                shares={shares?.data || []}
                agentId={agentId}
                onRevoke={handleShareRevoked}
              />
            </TabsContent>

            <TabsContent value="add" className="m-0">
              <AddShareForm
                agentId={agentId}
                onSuccess={handleShareCreated}
              />
            </TabsContent>

            <TabsContent value="updates" className="m-0">
              <ClonesList
                clones={clones || []}
                shares={shares?.data || []}
                agentId={agentId}
                onUpdatesPushed={handleUpdatesPushed}
              />
            </TabsContent>
          </div>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}
