'use client';

import { cn } from '@/lib/utils';

interface ScoreBarProps {
  score: number;
  size?: 'sm' | 'md' | 'lg';
  showLabel?: boolean;
  className?: string;
}

function getScoreColor(score: number): string {
  if (score >= 80) return 'bg-[#1D9E75]';
  if (score >= 60) return 'bg-[#D4853A]';
  return 'bg-[#C0392B]';
}

function getScoreTextColor(score: number): string {
  if (score >= 80) return 'text-[#1D9E75]';
  if (score >= 60) return 'text-[#D4853A]';
  return 'text-[#C0392B]';
}

const sizeMap = {
  sm: { bar: 'h-2', text: 'text-xs', width: 'w-20' },
  md: { bar: 'h-3', text: 'text-sm', width: 'w-28' },
  lg: { bar: 'h-4', text: 'text-base', width: 'w-36' },
};

export function ScoreBar({ score, size = 'md', showLabel = true, className = '' }: ScoreBarProps) {
  const clamped = Math.max(0, Math.min(100, score));
  const config = sizeMap[size];

  return (
    <div className={cn('flex items-center gap-2', className)}>
      {showLabel && (
        <span className={cn('font-semibold tabular-nums', config.text, getScoreTextColor(clamped))}>
          {clamped}
        </span>
      )}
      <div className={cn('rounded-full bg-muted overflow-hidden', config.width, config.bar)}>
        <div
          className={cn('h-full rounded-full transition-all duration-500', getScoreColor(clamped))}
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}
