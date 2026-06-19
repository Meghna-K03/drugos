'use client';

import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { pathwayData } from '@/lib/mock-data';

interface PathwayVizProps {
  className?: string;
}

const typeStyles: Record<string, { fill: string; stroke: string; label: string }> = {
  receptor: { fill: '#5B4FCF', stroke: '#4A3FB8', label: 'Receptor' },
  kinase: { fill: '#1D9E75', stroke: '#178060', label: 'Kinase' },
  transcription: { fill: '#D4853A', stroke: '#B07030', label: 'TF' },
  effector: { fill: '#8B5CF6', stroke: '#7048D4', label: 'Effector' },
  drug: { fill: '#C0392B', stroke: '#A03025', label: 'Drug' },
};

export function PathwayViz({ className = '' }: PathwayVizProps) {
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);

  const nodeMap = new Map(pathwayData.nodes.map((n) => [n.id, n]));
  const connectedEdges = selectedNode
    ? pathwayData.edges.filter((e) => e.source === selectedNode || e.target === selectedNode)
    : [];
  const connectedNodes = new Set<string>();
  connectedEdges.forEach((e) => { connectedNodes.add(e.source); connectedNodes.add(e.target); });

  return (
    <div className={`relative ${className}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-foreground">{pathwayData.name}</h3>
        <div className="flex gap-2">
          {Object.entries(typeStyles).map(([type, style]) => (
            <div key={type} className="flex items-center gap-1">
              <span
                className="h-2 w-2 rounded-full"
                style={{ backgroundColor: style.fill }}
              />
              <span className="text-[10px] text-muted-foreground">{style.label}</span>
            </div>
          ))}
        </div>
      </div>

      <svg
        width="100%"
        height="360"
        viewBox="0 0 750 360"
        className="bg-background border border-border rounded-lg"
      >
        {/* Arrow marker definitions */}
        <defs>
          <marker id="arrow-activation" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="#1D9E75" />
          </marker>
          <marker id="arrow-inhibition" markerWidth="8" markerHeight="8" refX="8" refY="4" orient="auto">
            <line x1="8" y1="0" x2="8" y2="8" stroke="#C0392B" strokeWidth="2" />
          </marker>
        </defs>

        {/* Edges */}
        {pathwayData.edges.map((edge, i) => {
          const source = nodeMap.get(edge.source);
          const target = nodeMap.get(edge.target);
          if (!source || !target) return null;

          const isHighlighted = selectedNode ? connectedNodes.has(edge.source) && connectedNodes.has(edge.target) : true;
          const isActivation = edge.type === 'activation';
          const isBinding = edge.type === 'binding';

          const dx = target.x - source.x;
          const dy = target.y - source.y;
          const angle = Math.atan2(dy, dx);
          const offset = 28;

          const x1 = source.x + Math.cos(angle) * offset;
          const y1 = source.y + Math.sin(angle) * offset;
          const x2 = target.x - Math.cos(angle) * offset;
          const y2 = target.y - Math.sin(angle) * offset;

          return (
            <g key={i} opacity={isHighlighted ? 1 : 0.2}>
              {isBinding ? (
                <line
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke="#5B4FCF"
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                />
              ) : (
                <line
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke={isActivation ? '#1D9E75' : '#C0392B'}
                  strokeWidth={1.5}
                  markerEnd={isActivation ? 'url(#arrow-activation)' : 'url(#arrow-inhibition)'}
                />
              )}
              <text
                x={(x1 + x2) / 2}
                y={(y1 + y2) / 2 - 6}
                textAnchor="middle"
                className="text-[8px] fill-muted-foreground pointer-events-none"
              >
                {edge.type}
              </text>
            </g>
          );
        })}

        {/* Nodes */}
        {pathwayData.nodes.map((node) => {
          const style = typeStyles[node.type];
          const isSelected = selectedNode === node.id;
          const isConnected = connectedNodes.has(node.id);
          const isActive = !selectedNode || isConnected || isSelected;
          const isHovered = hoveredNode === node.id;

          return (
            <g
              key={node.id}
              className="cursor-pointer"
              onClick={() => setSelectedNode(selectedNode === node.id ? null : node.id)}
              onMouseEnter={() => setHoveredNode(node.id)}
              onMouseLeave={() => setHoveredNode(null)}
              opacity={isActive ? 1 : 0.3}
            >
              {/* Background glow */}
              {(isSelected || isHovered) && (
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={24}
                  fill={style.fill}
                  fillOpacity={0.1}
                />
              )}
              {/* Node shape */}
              {node.type === 'drug' ? (
                <rect
                  x={node.x - 24}
                  y={node.y - 12}
                  width={48}
                  height={24}
                  rx={4}
                  fill={style.fill}
                  fillOpacity={0.15}
                  stroke={style.stroke}
                  strokeWidth={isSelected ? 2.5 : 1.5}
                />
              ) : (
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={20}
                  fill={style.fill}
                  fillOpacity={0.15}
                  stroke={style.stroke}
                  strokeWidth={isSelected ? 2.5 : 1.5}
                />
              )}
              {/* Label */}
              <text
                x={node.x}
                y={node.y + 4}
                textAnchor="middle"
                className="text-[10px] fill-foreground font-medium pointer-events-none"
              >
                {node.label}
              </text>
            </g>
          );
        })}
      </svg>

      {/* Selected node detail */}
      {selectedNode && nodeMap.has(selectedNode) && (
        <div className="mt-3 p-3 bg-muted/50 rounded-lg border border-border">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm">{nodeMap.get(selectedNode)?.label}</span>
            <Badge variant="secondary" className="text-xs">
              {typeStyles[nodeMap.get(selectedNode)?.type ?? 'effector']?.label ?? 'Node'}
            </Badge>
          </div>
          <p className="text-xs text-muted-foreground mt-1">
            {connectedEdges.length} connection{connectedEdges.length !== 1 ? 's' : ''} in this pathway
          </p>
        </div>
      )}
    </div>
  );
}
