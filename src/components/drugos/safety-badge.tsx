'use client';

import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import type { SafetyTier } from '@/lib/mock-data';

interface SafetyBadgeProps {
  tier: SafetyTier;
  showLabel?: boolean;
  className?: string;
}

const tierConfig: Record<SafetyTier, { label: string; className: string; dotClass: string }> = {
  green: {
    label: 'Safe',
    className: 'bg-[#1D9E75]/10 text-[#1D9E75] border-[#1D9E75]/20',
    dotClass: 'bg-[#1D9E75]',
  },
  yellow: {
    label: 'Caution',
    className: 'bg-[#D4853A]/10 text-[#D4853A] border-[#D4853A]/20',
    dotClass: 'bg-[#D4853A]',
  },
  red: {
    label: 'High Risk',
    className: 'bg-[#C0392B]/10 text-[#C0392B] border-[#C0392B]/20',
    dotClass: 'bg-[#C0392B]',
  },
};

export function SafetyBadge({ tier, showLabel = true, className = '' }: SafetyBadgeProps) {
  const config = tierConfig[tier];

  return (
    <Badge
      variant="outline"
      className={cn('gap-1.5 font-medium', config.className, className)}
    >
      <span className={cn('h-2 w-2 rounded-full', config.dotClass)} />
      {showLabel && config.label}
    </Badge>
  );
}
