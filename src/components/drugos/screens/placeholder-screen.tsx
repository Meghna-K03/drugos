'use client';

import { getScreenMeta, sidebarCategories } from '@/lib/screens';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ArrowRight } from 'lucide-react';

interface PlaceholderScreenProps {
  screenId: string;
}

export function PlaceholderScreen({ screenId }: PlaceholderScreenProps) {
  const meta = getScreenMeta(screenId);
  const categoryItems = sidebarCategories.find((c) => c.id === meta?.category);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-foreground">{meta?.name ?? screenId}</h1>
        <p className="text-sm text-muted-foreground mt-1">{meta?.description ?? 'Screen under development'}</p>
        <div className="flex items-center gap-2 mt-2">
          <Badge variant="outline" className="font-mono text-xs">{screenId}</Badge>
          <Badge variant="secondary" className="text-xs">{meta?.category ?? 'Unknown'}</Badge>
        </div>
      </div>

      <Card>
        <CardContent className="p-8">
          <div className="text-center max-w-md mx-auto">
            <div className="h-16 w-16 rounded-2xl bg-gradient-to-br from-primary/10 to-primary/5 flex items-center justify-center mx-auto mb-4">
              <span className="text-2xl">🚧</span>
            </div>
            <h3 className="text-lg font-semibold mb-2">Screen Under Development</h3>
            <p className="text-sm text-muted-foreground mb-6">
              The <strong>{meta?.name ?? screenId}</strong> screen is currently being built. 
              Check back soon for the full interactive experience.
            </p>
            {categoryItems && categoryItems.items.length > 0 && (
              <div>
                <p className="text-xs text-muted-foreground mb-3">Other screens in {meta?.category}:</p>
                <div className="flex flex-wrap gap-2 justify-center">
                  {categoryItems.items
                    .filter((i) => i.id !== screenId)
                    .slice(0, 6)
                    .map((item) => (
                      <Badge key={item.id} variant="outline" className="text-xs">
                        {item.id}: {item.name}
                      </Badge>
                    ))}
                </div>
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
