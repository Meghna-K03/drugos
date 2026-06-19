'use client';

import { useState } from 'react';
import { ChevronDown, ChevronUp, ExternalLink, Star } from 'lucide-react';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { SafetyBadge } from '@/components/drugos/safety-badge';
import { ScoreBar } from '@/components/drugos/score-bar';
import type { DrugCandidate } from '@/lib/mock-data';

interface CandidateTableProps {
  candidates: DrugCandidate[];
  onSelect?: (candidate: DrugCandidate) => void;
  onCompare?: (candidate: DrugCandidate) => void;
  showDiseaseColumn?: boolean;
  className?: string;
}

export function CandidateTable({
  candidates,
  onSelect,
  onCompare,
  showDiseaseColumn = true,
  className = '',
}: CandidateTableProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [shortlisted, setShortlisted] = useState<Set<string>>(new Set());

  const toggleExpand = (id: string) => {
    setExpandedId((prev) => (prev === id ? null : id));
  };

  const toggleShortlist = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setShortlisted((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className={`rounded-lg border border-border overflow-hidden ${className}`}>
      <Table>
        <TableHeader>
          <TableRow className="bg-muted/50 hover:bg-muted/50">
            <TableHead className="w-10"></TableHead>
            <TableHead className="w-10"></TableHead>
            <TableHead>Drug Name</TableHead>
            <TableHead>Composite Score</TableHead>
            <TableHead>Safety</TableHead>
            <TableHead>Mechanism</TableHead>
            {showDiseaseColumn && <TableHead>Disease</TableHead>}
            <TableHead>Phase</TableHead>
            <TableHead>Confidence</TableHead>
            <TableHead className="w-10"></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {candidates.map((candidate) => {
            const isExpanded = expandedId === candidate.id;
            const isShortlisted = shortlisted.has(candidate.id);

            return (
              <TableRow
                key={candidate.id}
                className="cursor-pointer hover:bg-muted/30"
                onClick={() => onSelect?.(candidate)}
              >
                <TableCell>
                  <button onClick={(e) => toggleShortlist(candidate.id, e)} className="focus:outline-none">
                    <Star
                      className={`h-4 w-4 transition-colors ${
                        isShortlisted ? 'fill-yellow-400 text-yellow-400' : 'text-muted-foreground hover:text-yellow-400'
                      }`}
                    />
                  </button>
                </TableCell>
                <TableCell>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleExpand(candidate.id);
                    }}
                    className="focus:outline-none"
                  >
                    {isExpanded ? (
                      <ChevronUp className="h-4 w-4 text-muted-foreground" />
                    ) : (
                      <ChevronDown className="h-4 w-4 text-muted-foreground" />
                    )}
                  </button>
                </TableCell>
                <TableCell>
                  <div>
                    <div className="font-medium text-foreground">{candidate.name}</div>
                    <div className="text-xs text-muted-foreground">{candidate.genericName}</div>
                  </div>
                </TableCell>
                <TableCell>
                  <ScoreBar score={candidate.compositeScore} size="sm" />
                </TableCell>
                <TableCell>
                  <SafetyBadge tier={candidate.safetyTier} />
                </TableCell>
                <TableCell>
                  <span className="text-sm max-w-[200px] truncate block">{candidate.mechanism}</span>
                </TableCell>
                {showDiseaseColumn && (
                  <TableCell>
                    <span className="text-sm">{candidate.diseaseName}</span>
                  </TableCell>
                )}
                <TableCell>
                  <Badge variant="outline" className="text-xs">{candidate.phase}</Badge>
                </TableCell>
                <TableCell>
                  <span className="text-sm font-medium">{candidate.repurposingConfidence}%</span>
                </TableCell>
                <TableCell>
                  <Button variant="ghost" size="sm" className="h-7 w-7 p-0">
                    <ExternalLink className="h-3.5 w-3.5" />
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
