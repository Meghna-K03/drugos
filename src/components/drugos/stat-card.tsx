'use client';

import type { LucideIcon } from 'lucide-react';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

interface StatCardProps {
  icon: LucideIcon;
  value: string | number;
  label: string;
  trend?: { value: number; label?: string };
  className?: string;
  iconColor?: string;
}

export function StatCard({
  icon: Icon,
  value,
  label,
  trend,
  className = '',
  iconColor = 'text-primary',
}: StatCardProps) {
  const isPositive = trend && trend.value > 0;
  const isNegative = trend && trend.value < 0;
  const isNeutral = trend && trend.value === 0;

  return (
    <Card className={cn('hover:shadow-md transition-shadow', className)}>
      <CardContent className="p-4">
        <div className="flex items-start justify-between">
          <div className="flex-1 min-w-0">
            <p className="text-sm text-muted-foreground mb-1">{label}</p>
            <p className="text-2xl font-bold text-foreground tabular-nums">{value}</p>
            {trend && (
              <div className="flex items-center gap-1 mt-1">
                {isPositive && <TrendingUp className="h-3.5 w-3.5 text-[#1D9E75]" />}
                {isNegative && <TrendingDown className="h-3.5 w-3.5 text-[#C0392B]" />}
                {isNeutral && <Minus className="h-3.5 w-3.5 text-muted-foreground" />}
                <span
                  className={cn(
                    'text-xs font-medium',
                    isPositive && 'text-[#1D9E75]',
                    isNegative && 'text-[#C0392B]',
                    isNeutral && 'text-muted-foreground'
                  )}
                >
                  {isPositive && '+'}
                  {trend.value}%
                </span>
                {trend.label && (
                  <span className="text-xs text-muted-foreground">{trend.label}</span>
                )}
              </div>
            )}
          </div>
          <div className={cn('rounded-lg p-2.5 bg-primary/10', iconColor)}>
            <Icon className="h-5 w-5" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
