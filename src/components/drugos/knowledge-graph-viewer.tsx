'use client';

import { useState, useCallback } from 'react';
import { ZoomIn, ZoomOut, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import type { KnowledgeGraphNode, KnowledgeGraphEdge } from '@/lib/mock-data';
import { knowledgeGraphNodes, knowledgeGraphEdges } from '@/lib/mock-data';

interface KnowledgeGraphViewerProps {
  nodes?: KnowledgeGraphNode[];
  edges?: KnowledgeGraphEdge[];
  width?: number;
  height?: number;
  className?: string;
}

const typeColors: Record<string, string> = {
  disease: '#C0392B',
  drug: '#1D9E75',
  gene: '#5B4FCF',
  pathway: '#D4853A',
  protein: '#8B5CF6',
  phenotype: '#C0392B',
};

const typeLabels: Record<string, string> = {
  disease: 'Disease',
  drug: 'Drug',
  gene: 'Gene',
  pathway: 'Pathway',
  protein: 'Protein',
  phenotype: 'Phenotype',
};

function buildPositionMap(nodes: KnowledgeGraphNode[]): Map<string, { x: number; y: number }> {
  return new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
}

export function KnowledgeGraphViewer({
  nodes = knowledgeGraphNodes,
  edges = knowledgeGraphEdges,
  width = 900,
  height = 550,
  className = '',
}: KnowledgeGraphViewerProps) {
  const [zoom, setZoom] = useState(1);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [dragNode, setDragNode] = useState<string | null>(null);
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }>>(
    () => buildPositionMap(nodes)
  );

  const handleZoomIn = () => setZoom((z) => Math.min(z + 0.2, 3));
  const handleZoomOut = () => setZoom((z) => Math.max(z - 0.2, 0.3));
  const handleReset = () => {
    setZoom(1);
    setSelectedNode(null);
    setPositions(buildPositionMap(nodes));
  };

  const handleNodeMouseDown = useCallback((nodeId: string) => {
    setDragNode(nodeId);
    setSelectedNode(nodeId);
  }, []);

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (!dragNode) return;
      const svg = e.currentTarget;
      const rect = svg.getBoundingClientRect();
      const x = (e.clientX - rect.left - width / 2) / zoom + width / 2;
      const y = (e.clientY - rect.top - height / 2) / zoom + height / 2;
      setPositions((prev) => {
        const next = new Map(prev);
        next.set(dragNode, { x, y });
        return next;
      });
    },
    [dragNode, zoom, width, height]
  );

  const handleMouseUp = useCallback(() => {
    setDragNode(null);
  }, []);

  const getNodePos = (id: string) => positions.get(id) ?? { x: 0, y: 0 };

  const highlightedNodes = new Set<string>();
  if (selectedNode) {
    highlightedNodes.add(selectedNode);
    edges.forEach((edge) => {
      if (edge.source === selectedNode) highlightedNodes.add(edge.target);
      if (edge.target === selectedNode) highlightedNodes.add(edge.source);
    });
  }

  return (
    <div className={`relative bg-background border border-border rounded-lg overflow-hidden ${className}`}>
      {/* Controls */}
      <div className="absolute top-3 right-3 z-10 flex items-center gap-1">
        <Button variant="outline" size="sm" onClick={handleZoomOut} className="h-8 w-8 p-0">
          <ZoomOut className="h-4 w-4" />
        </Button>
        <span className="text-xs text-muted-foreground w-12 text-center tabular-nums">
          {Math.round(zoom * 100)}%
        </span>
        <Button variant="outline" size="sm" onClick={handleZoomIn} className="h-8 w-8 p-0">
          <ZoomIn className="h-4 w-4" />
        </Button>
        <Button variant="outline" size="sm" onClick={handleReset} className="h-8 w-8 p-0 ml-1">
          <RotateCcw className="h-4 w-4" />
        </Button>
      </div>

      {/* Legend */}
      <div className="absolute bottom-3 left-3 z-10 flex flex-wrap gap-2 bg-background/90 backdrop-blur-sm rounded-lg p-2 border border-border">
        {Object.entries(typeLabels).map(([type, label]) => (
          <div key={type} className="flex items-center gap-1.5">
            <span
              className="h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: typeColors[type] }}
            />
            <span className="text-xs text-muted-foreground">{label}</span>
          </div>
        ))}
      </div>

      <TooltipProvider>
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          className="w-full h-auto"
        >
          <g transform={`translate(${width / 2}, ${height / 2}) scale(${zoom}) translate(${-width / 2}, ${-height / 2})`}>
            {/* Edges */}
            {edges.map((edge) => {
              const source = getNodePos(edge.source);
              const target = getNodePos(edge.target);
              const isHighlighted = selectedNode
                ? edge.source === selectedNode || edge.target === selectedNode
                : true;
              return (
                <g key={edge.id}>
                  <line
                    x1={source.x}
                    y1={source.y}
                    x2={target.x}
                    y2={target.y}
                    stroke={isHighlighted ? '#5B4FCF' : '#E2E1EA'}
                    strokeWidth={isHighlighted ? 2 : 1}
                    strokeOpacity={isHighlighted ? 0.6 : 0.3}
                  />
                  {isHighlighted && (
                    <text
                      x={(source.x + target.x) / 2}
                      y={(source.y + target.y) / 2 - 6}
                      textAnchor="middle"
                      className="text-[9px] fill-muted-foreground"
                    >
                      {edge.label}
                    </text>
                  )}
                </g>
              );
            })}

            {/* Nodes */}
            {nodes.map((node) => {
              const pos = getNodePos(node.id);
              const isSelected = selectedNode === node.id;
              const isConnected = highlightedNodes.has(node.id);
              const isActive = !selectedNode || isConnected;
              return (
                <Tooltip key={node.id}>
                  <TooltipTrigger asChild>
                    <g
                      className="cursor-pointer"
                      onMouseDown={() => handleNodeMouseDown(node.id)}
                      opacity={isActive ? 1 : 0.3}
                    >
                      {/* Outer ring for selected */}
                      {isSelected && (
                        <circle
                          cx={pos.x}
                          cy={pos.y}
                          r={node.size + 6}
                          fill="none"
                          stroke={typeColors[node.type]}
                          strokeWidth={2}
                          strokeDasharray="4 2"
                          opacity={0.5}
                        />
                      )}
                      {/* Node circle */}
                      <circle
                        cx={pos.x}
                        cy={pos.y}
                        r={node.size}
                        fill={typeColors[node.type]}
                        fillOpacity={isConnected ? 0.2 : 0.1}
                        stroke={typeColors[node.type]}
                        strokeWidth={isSelected ? 3 : 2}
                      />
                      {/* Label */}
                      <text
                        x={pos.x}
                        y={pos.y + node.size + 14}
                        textAnchor="middle"
                        className="text-[10px] fill-foreground font-medium pointer-events-none"
                      >
                        {node.label}
                      </text>
                    </g>
                  </TooltipTrigger>
                  <TooltipContent>
                    <div className="text-xs">
                      <div className="font-semibold">{node.label}</div>
                      <Badge
                        variant="secondary"
                        className="mt-1 text-[10px]"
                        style={{ color: typeColors[node.type] }}
                      >
                        {typeLabels[node.type]}
                      </Badge>
                    </div>
                  </TooltipContent>
                </Tooltip>
              );
            })}
          </g>
        </svg>
      </TooltipProvider>

      {/* Selected Node Info */}
      {selectedNode && (
        <div className="absolute top-3 left-3 bg-background/90 backdrop-blur-sm border border-border rounded-lg p-3 max-w-[240px]">
          <div className="flex items-center justify-between mb-1">
            <span className="font-semibold text-sm">
              {nodes.find((n) => n.id === selectedNode)?.label}
            </span>
            <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => setSelectedNode(null)}>
              ×
            </Button>
          </div>
          <div className="text-xs text-muted-foreground">
            {edges.filter((e) => e.source === selectedNode || e.target === selectedNode).length} connections
          </div>
        </div>
      )}
    </div>
  );
}
