import { useEffect, useMemo } from 'react'
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { StoryGraph } from './api'

// Renders a story's passages as nodes and choices as edges. Read-only for now;
// editing lands once the model settles.
export function StoryCanvas({ graph }: { graph: StoryGraph }) {
  const { initialNodes, initialEdges } = useMemo(() => {
    const initialNodes: Node[] = graph.passages.map((p) => ({
      id: String(p.id),
      position: { x: p.pos_x, y: p.pos_y },
      data: { label: p.is_start ? `▶ ${p.title}` : p.title },
      type: p.is_start ? 'input' : undefined,
    }))
    const initialEdges: Edge[] = graph.choices
      .filter((c) => c.to_passage !== null)
      .map((c) => ({
        id: `c${c.id}`,
        source: String(c.from_passage),
        target: String(c.to_passage),
        label: c.text,
      }))
    return { initialNodes, initialEdges }
  }, [graph])

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges)
  useEffect(() => setNodes(initialNodes), [initialNodes, setNodes])
  useEffect(() => setEdges(initialEdges), [initialEdges, setEdges])

  return (
    <ReactFlow nodes={nodes} edges={edges} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} fitView>
      <Background />
      <Controls />
      <MiniMap />
    </ReactFlow>
  )
}
