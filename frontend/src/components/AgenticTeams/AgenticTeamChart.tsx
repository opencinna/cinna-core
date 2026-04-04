import { useCallback, useEffect, useRef, useState } from "react"
import {
  ReactFlow,
  Background,
  addEdge,
  useNodesState,
  useEdgesState,
  MarkerType,
  type Connection,
} from "@xyflow/react"
import dagre from "@dagrejs/dagre"
import "@xyflow/react/dist/style.css"
import { LayoutGrid } from "lucide-react"
import { Button } from "@/components/ui/button"
import { AgenticTeamChartNode, type AgenticTeamNodeData } from "./AgenticTeamChartNode"
import { AgenticTeamChartEdge } from "./AgenticTeamChartEdge"
import { ConnectionEditDialog } from "./ConnectionEditDialog"
import { AddNodeDialog } from "./AddNodeDialog"
import type {
  AgenticTeamNodePublic,
  AgenticTeamConnectionPublic,
  AgenticTeamNodePositionUpdate,
} from "@/client"

const NODE_WIDTH = 250
const NODE_HEIGHT = 60

const nodeTypes = { agenticTeamNode: AgenticTeamChartNode }
const edgeTypes = { agenticTeamEdge: AgenticTeamChartEdge }

function buildFlowNodes(
  nodes: AgenticTeamNodePublic[],
  isEditMode: boolean,
  onSetLead: (nodeId: string) => void,
  onDeleteNode: (nodeId: string) => void,
) {
  return nodes.map((n) => ({
    id: n.id,
    type: "agenticTeamNode",
    position: { x: n.pos_x, y: n.pos_y },
    data: {
      ...n,
      isEditMode,
      onSetLead,
      onDelete: onDeleteNode,
    } as AgenticTeamNodeData,
  }))
}

function buildFlowEdges(
  connections: AgenticTeamConnectionPublic[],
  isEditMode: boolean,
  onEditEdge: (edgeId: string) => void,
  onDeleteEdge: (edgeId: string) => void,
) {
  return connections.map((c) => ({
    id: c.id,
    source: c.source_node_id,
    target: c.target_node_id,
    type: "agenticTeamEdge",
    markerEnd: { type: MarkerType.ArrowClosed },
    data: {
      isEditMode,
      enabled: c.enabled,
      onEdit: onEditEdge,
      onDelete: onDeleteEdge,
    },
  }))
}

function autoArrangeLayout(
  nodes: AgenticTeamNodePublic[],
  connections: AgenticTeamConnectionPublic[],
): Array<{ id: string; pos_x: number; pos_y: number }> {
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: "TB", nodesep: 80, ranksep: 120 })

  nodes.forEach((n) => {
    g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT })
  })
  connections.forEach((c) => {
    g.setEdge(c.source_node_id, c.target_node_id)
  })

  dagre.layout(g)

  return nodes.map((n) => {
    const node = g.node(n.id)
    return {
      id: n.id,
      pos_x: node.x - NODE_WIDTH / 2,
      pos_y: node.y - NODE_HEIGHT / 2,
    }
  })
}

interface AgenticTeamChartProps {
  teamId: string
  nodes: AgenticTeamNodePublic[]
  connections: AgenticTeamConnectionPublic[]
  isEditMode: boolean
  onCreateNode: (agentId: string, isLead: boolean) => void
  onUpdateNode: (nodeId: string, updates: { is_lead?: boolean }) => void
  onDeleteNode: (nodeId: string) => void
  onBulkUpdatePositions: (positions: AgenticTeamNodePositionUpdate[]) => void
  onCreateConnection: (sourceId: string, targetId: string) => void
  onUpdateConnection: (connId: string, prompt: string, enabled: boolean) => void
  onDeleteConnection: (connId: string) => void
  onGenerateConnectionPrompt: (connId: string) => void
  isCreatingNode: boolean
  isCreatingConnection: boolean
  isGeneratingPrompt: boolean
  generatedPrompt: string | null
}

export function AgenticTeamChart({
  nodes,
  connections,
  isEditMode,
  onCreateNode,
  onUpdateNode,
  onDeleteNode,
  onBulkUpdatePositions,
  onCreateConnection,
  onUpdateConnection,
  onDeleteConnection,
  onGenerateConnectionPrompt,
  isCreatingNode,
  isCreatingConnection,
  isGeneratingPrompt,
  generatedPrompt,
}: AgenticTeamChartProps) {
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [showAddNode, setShowAddNode] = useState(false)
  const [editingConnection, setEditingConnection] =
    useState<AgenticTeamConnectionPublic | null>(null)

  const handleSetLead = useCallback(
    (nodeId: string) => {
      const node = nodes.find((n) => n.id === nodeId)
      if (!node) return
      onUpdateNode(nodeId, { is_lead: !node.is_lead })
    },
    [nodes, onUpdateNode],
  )

  const handleDeleteNode = useCallback(
    (nodeId: string) => {
      onDeleteNode(nodeId)
    },
    [onDeleteNode],
  )

  const handleEditEdge = useCallback(
    (edgeId: string) => {
      const conn = connections.find((c) => c.id === edgeId)
      if (conn) setEditingConnection(conn)
    },
    [connections],
  )

  const handleDeleteEdge = useCallback(
    (edgeId: string) => {
      onDeleteConnection(edgeId)
    },
    [onDeleteConnection],
  )

  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState(
    buildFlowNodes(nodes, isEditMode, handleSetLead, handleDeleteNode),
  )
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState(
    buildFlowEdges(connections, isEditMode, handleEditEdge, handleDeleteEdge),
  )

  // Sync external data changes into flow state, preserving local positions
  useEffect(() => {
    setFlowNodes((currentFlowNodes) => {
      const positionMap = new Map(
        currentFlowNodes.map((n) => [n.id, n.position]),
      )
      return buildFlowNodes(nodes, isEditMode, handleSetLead, handleDeleteNode).map(
        (n) => {
          const existingPos = positionMap.get(n.id)
          if (existingPos) {
            return { ...n, position: existingPos }
          }
          return n
        },
      )
    })
  }, [nodes, isEditMode, handleSetLead, handleDeleteNode, setFlowNodes])

  useEffect(() => {
    setFlowEdges(
      buildFlowEdges(connections, isEditMode, handleEditEdge, handleDeleteEdge),
    )
  }, [connections, isEditMode, handleEditEdge, handleDeleteEdge, setFlowEdges])

  // Debounced position save after drag
  const handleNodesChange = useCallback(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (changes: any[]) => {
      onNodesChange(changes)

      const hasDragChange = changes.some(
        (c) => c.type === "position" && !c.dragging,
      )
      if (hasDragChange && isEditMode) {
        if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current)
        debounceTimerRef.current = setTimeout(() => {
          setFlowNodes((nds) => {
            const positions: AgenticTeamNodePositionUpdate[] = nds.map((n) => ({
              id: n.id,
              pos_x: n.position.x,
              pos_y: n.position.y,
            }))
            onBulkUpdatePositions(positions)
            return nds
          })
        }, 300)
      }
    },
    [onNodesChange, isEditMode, setFlowNodes, onBulkUpdatePositions],
  )

  const handleEdgesChange = useCallback(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (changes: any[]) => {
      onEdgesChange(changes)
    },
    [onEdgesChange],
  )

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!isEditMode || !connection.source || !connection.target) return
      onCreateConnection(connection.source, connection.target)
      setFlowEdges((eds) => addEdge(connection, eds))
    },
    [isEditMode, onCreateConnection, setFlowEdges],
  )

  const handleAutoArrange = useCallback(() => {
    const arranged = autoArrangeLayout(nodes, connections)
    setFlowNodes((nds) =>
      nds.map((n) => {
        const pos = arranged.find((a) => a.id === n.id)
        if (!pos) return n
        return { ...n, position: { x: pos.pos_x, y: pos.pos_y } }
      }),
    )
    onBulkUpdatePositions(
      arranged.map((a) => ({ id: a.id, pos_x: a.pos_x, pos_y: a.pos_y })),
    )
  }, [nodes, connections, setFlowNodes, onBulkUpdatePositions])

  const existingAgentIds = nodes.map((n) => n.agent_id)

  return (
    <div className="relative w-full h-full">
      {/* Auto-arrange button overlay */}
      <div className="absolute top-3 right-3 z-10 flex gap-2">
        <Button
          variant="secondary"
          size="sm"
          onClick={handleAutoArrange}
          title="Auto-arrange nodes"
          className="shadow-sm"
        >
          <LayoutGrid className="h-4 w-4 mr-1.5" />
          Auto-Arrange
        </Button>
        {isEditMode && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowAddNode(true)}
            className="shadow-sm"
          >
            + Add Node
          </Button>
        )}
      </div>

      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onConnect={handleConnect}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesDraggable={isEditMode}
        nodesConnectable={isEditMode}
        elementsSelectable={isEditMode}
        fitView
        fitViewOptions={{ padding: 0.2 }}
      >
        {isEditMode && <Background />}
      </ReactFlow>

      {nodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <div className="text-center text-muted-foreground">
            <p className="text-sm">No nodes yet.</p>
            <p className="text-xs mt-1">Switch to edit mode to add team members.</p>
          </div>
        </div>
      )}

      {/* Add Node Dialog */}
      <AddNodeDialog
        open={showAddNode}
        onClose={() => setShowAddNode(false)}
        existingAgentIds={existingAgentIds}
        onAdd={(agentId, lead) => {
          onCreateNode(agentId, lead)
          setShowAddNode(false)
        }}
        isPending={isCreatingNode}
      />

      {/* Edit Connection Dialog */}
      <ConnectionEditDialog
        open={!!editingConnection}
        onClose={() => setEditingConnection(null)}
        connection={editingConnection}
        onSave={(connId, prompt, enabled) => {
          onUpdateConnection(connId, prompt, enabled)
          setEditingConnection(null)
        }}
        onGenerate={(connId) => onGenerateConnectionPrompt(connId)}
        isPending={isCreatingConnection}
        isGenerating={isGeneratingPrompt}
        generatedPrompt={generatedPrompt}
      />
    </div>
  )
}
