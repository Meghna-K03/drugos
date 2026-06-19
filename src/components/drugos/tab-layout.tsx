'use client';

import { useState, type ReactNode } from 'react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';

interface TabItem {
  id: string;
  label: string;
  icon?: ReactNode;
  content: ReactNode;
  badge?: string | number;
}

interface TabLayoutProps {
  tabs: TabItem[];
  defaultTab?: string;
  className?: string;
  onChange?: (tabId: string) => void;
}

export function TabLayout({
  tabs,
  defaultTab,
  className = '',
  onChange,
}: TabLayoutProps) {
  const [activeTab, setActiveTab] = useState(defaultTab ?? tabs[0]?.id ?? '');

  const handleTabChange = (value: string) => {
    setActiveTab(value);
    onChange?.(value);
  };

  return (
    <Tabs
      value={activeTab}
      onValueChange={handleTabChange}
      className={cn('w-full', className)}
    >
      <TabsList className="w-full justify-start h-auto p-1 bg-muted/50 rounded-lg">
        {tabs.map((tab) => (
          <TabsTrigger
            key={tab.id}
            value={tab.id}
            className="gap-1.5 data-[state=active]:bg-background data-[state=active]:shadow-sm"
          >
            {tab.icon}
            {tab.label}
            {tab.badge != null && (
              <span className="ml-1 px-1.5 py-0.5 text-[10px] font-medium bg-primary/10 text-primary rounded-full">
                {tab.badge}
              </span>
            )}
          </TabsTrigger>
        ))}
      </TabsList>

      {tabs.map((tab) => (
        <TabsContent key={tab.id} value={tab.id} className="mt-4">
          {tab.content}
        </TabsContent>
      ))}
    </Tabs>
  );
}
