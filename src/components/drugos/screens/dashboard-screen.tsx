'use client';

import {
  LayoutDashboard,
  BarChart3,
  Activity,
  TrendingUp,
  Target,
  Bell,
  Users,
  FileText,
  GitBranch,
  Heart,
  Shield,
  Database,
  Building2,
  FolderKanban,
  LineChart,
  Briefcase,
} from 'lucide-react';
import { StatCard } from '@/components/drugos/stat-card';
import { ScoreBar } from '@/components/drugos/score-bar';
import { SafetyBadge } from '@/components/drugos/safety-badge';
import { CandidateTable } from '@/components/drugos/candidate-table';
import { DiseaseSearchBar } from '@/components/drugos/disease-search-bar';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  dashboardStats,
  drugCandidates,
  diseases,
  recentActivity,
  milestones,
  monthlyQueryTrend,
  safetyTierDistribution,
} from '@/lib/mock-data';

interface DashboardScreenProps {
  screenId: string;
}

export function DashboardScreen({ screenId }: DashboardScreenProps) {
  switch (screenId) {
    case 'DASH-01':
      return <PersonalDashboard />;
    case 'DASH-02':
      return <OrgDashboard />;
    case 'DASH-03':
      return <ProjectDashboard />;
    case 'DASH-04':
      return <UsageAnalytics />;
    case 'DASH-07':
      return <PipelineAnalytics />;
    case 'DASH-10':
      return <AlertCenter />;
    default:
      return <GenericDashboard screenId={screenId} />;
  }
}

// ---- Personal Dashboard (DASH-01) ----

function PersonalDashboard() {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Personal Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Welcome back, Dr. Chen. Here&apos;s your research overview.
        </p>
      </div>

      {/* Quick Search */}
      <DiseaseSearchBar
        onSearch={(q) => {
          const url = new URL(window.location.href);
          url.searchParams.set('screen', 'CORE-02');
          url.searchParams.set('q', q);
          window.history.pushState({}, '', url.toString());
        }}
      />

      {/* Stats Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={Pill} value={dashboardStats.totalCandidates} label="Total Candidates" trend={{ value: 12, label: 'this month' }} />
        <StatCard icon={Activity} value={dashboardStats.clinicalTrials} label="Clinical Trials" trend={{ value: 8, label: 'this month' }} />
        <StatCard icon={BarChart3} value={dashboardStats.queriesThisMonth} label="Queries This Month" trend={{ value: -3, label: 'vs last' }} />
        <StatCard icon={FileText} value={dashboardStats.reportsGenerated} label="Reports Generated" trend={{ value: 15, label: 'this month' }} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Top Candidates */}
        <div className="lg:col-span-2">
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="text-base">Top Candidates</CardTitle>
                  <CardDescription>Highest-ranked repurposing candidates</CardDescription>
                </div>
                <Badge variant="outline" className="text-xs">{drugCandidates.length} total</Badge>
              </div>
            </CardHeader>
            <CardContent>
              <div className="space-y-3">
                {drugCandidates.slice(0, 5).map((c) => (
                  <div
                    key={c.id}
                    className="flex items-center gap-3 p-3 rounded-lg border border-border hover:bg-accent/50 transition-colors cursor-pointer"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm">{c.name}</span>
                        <SafetyBadge tier={c.safetyTier} showLabel={false} />
                      </div>
                      <div className="text-xs text-muted-foreground mt-0.5">
                        {c.diseaseName} · {c.mechanism}
                      </div>
                    </div>
                    <ScoreBar score={c.compositeScore} size="sm" />
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Safety Distribution */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Safety Tier Distribution</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {safetyTierDistribution.map((tier) => (
                <div key={tier.tier} className="space-y-1.5">
                  <div className="flex items-center justify-between text-sm">
                    <span className="flex items-center gap-2">
                      <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: tier.fill }} />
                      {tier.tier}
                    </span>
                    <span className="font-semibold">{tier.count}</span>
                  </div>
                  <Progress
                    value={(tier.count / dashboardStats.totalCandidates) * 100}
                    className="h-2"
                  />
                </div>
              ))}
            </div>

            <div className="mt-6 pt-4 border-t border-border">
              <h4 className="text-sm font-medium mb-3">Query Trend</h4>
              <div className="flex items-end gap-1.5 h-20">
                {monthlyQueryTrend.map((m) => (
                  <div key={m.month} className="flex-1 flex flex-col items-center gap-1">
                    <div
                      className="w-full bg-primary/20 rounded-t"
                      style={{ height: `${(m.queries / 400) * 100}%` }}
                    />
                    <span className="text-[9px] text-muted-foreground">{m.month}</span>
                  </div>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Recent Activity + Milestones */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Activity */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Recent Activity</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {recentActivity.slice(0, 6).map((item) => {
                const iconMap: Record<string, string> = {
                  query: '🔍',
                  candidate: '💊',
                  report: '📊',
                  safety: '⚠️',
                  team: '👥',
                  data: '🗄️',
                };
                return (
                  <div key={item.id} className="flex items-start gap-3">
                    <span className="text-base mt-0.5">{iconMap[item.type] ?? '📌'}</span>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm">
                        <span className="font-medium">{item.user}</span>{' '}
                        <span className="text-muted-foreground">{item.action}</span>{' '}
                        <span className="font-medium">{item.target}</span>
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {new Date(item.timestamp).toLocaleString()}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>

        {/* Milestones */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Active Milestones</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {milestones.filter(m => m.status !== 'completed').slice(0, 5).map((ms) => (
                <div key={ms.id} className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{ms.title}</span>
                    <Badge
                      variant={ms.status === 'overdue' ? 'destructive' : ms.status === 'in_progress' ? 'default' : 'secondary'}
                      className="text-[10px]"
                    >
                      {ms.status.replace('_', ' ')}
                    </Badge>
                  </div>
                  <Progress value={ms.progress} className="h-1.5" />
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>{ms.assignee}</span>
                    <span>Due: {ms.dueDate}</span>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Diseases Quick Access */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">Diseases Being Tracked</CardTitle>
              <CardDescription>Active disease research programs</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {diseases.slice(0, 6).map((d) => (
              <div
                key={d.id}
                className="p-3 rounded-lg border border-border hover:bg-accent/50 transition-colors cursor-pointer"
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="font-medium text-sm">{d.name}</span>
                  <Badge variant="secondary" className="text-[10px]">{d.icd10}</Badge>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-1">{d.category}</p>
                <div className="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
                  <span>{d.candidateCount} candidates</span>
                  <span>{d.clinicalTrialCount} trials</span>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- Organization Dashboard (DASH-02) ----

function OrgDashboard() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Organization Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">NeuroGen Therapeutics — Organization overview</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={Users} value={26} label="Team Members" trend={{ value: 8 }} />
        <StatCard icon={GitBranch} value={18} label="Active Projects" trend={{ value: 12 }} />
        <StatCard icon={FileText} value={342} label="Total Queries" trend={{ value: -5 }} />
        <StatCard icon={TrendingUp} value={87} label="Reports Generated" trend={{ value: 23 }} />
      </div>
      <Card>
        <CardHeader><CardTitle className="text-base">Organization Activity</CardTitle></CardHeader>
        <CardContent>
          <div className="space-y-3">
            {recentActivity.slice(0, 8).map((item) => (
              <div key={item.id} className="flex items-center gap-3 p-2 rounded-md hover:bg-accent/50">
                <div className="h-8 w-8 rounded-full bg-primary/10 flex items-center justify-center text-xs font-bold text-primary">
                  {item.user.split(' ').map(n => n[0]).join('').slice(0, 2)}
                </div>
                <div className="flex-1">
                  <p className="text-sm"><span className="font-medium">{item.user}</span> {item.action} <span className="font-medium">{item.target}</span></p>
                  <p className="text-xs text-muted-foreground">{new Date(item.timestamp).toLocaleString()}</p>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- Project Dashboard (DASH-03) ----

function ProjectDashboard() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Project Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Track project progress and milestones</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard icon={FolderKanban} value={18} label="Active Projects" />
        <StatCard icon={Target} value={7} label="Milestones Due This Week" />
        <StatCard icon={Activity} value={72} label="Avg. Completion %" />
      </div>
      <Card>
        <CardHeader><CardTitle className="text-base">Project Milestones</CardTitle></CardHeader>
        <CardContent>
          <div className="space-y-4">
            {milestones.map((ms) => (
              <div key={ms.id} className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{ms.title}</span>
                  <Badge variant={ms.status === 'overdue' ? 'destructive' : ms.status === 'completed' ? 'secondary' : 'default'} className="text-[10px]">
                    {ms.status.replace('_', ' ')}
                  </Badge>
                </div>
                <Progress value={ms.progress} className="h-2" />
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span>{ms.assignee} · {ms.project}</span>
                  <span>Due: {ms.dueDate}</span>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- Usage Analytics (DASH-04) ----

function UsageAnalytics() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Usage Analytics</h1>
        <p className="text-sm text-muted-foreground mt-1">Query trends and platform usage statistics</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={BarChart3} value={342} label="Queries This Month" trend={{ value: 15 }} />
        <StatCard icon={Users} value={26} label="Active Users" trend={{ value: 8 }} />
        <StatCard icon={FileText} value={87} label="Reports Generated" trend={{ value: 23 }} />
        <StatCard icon={Database} value={8} label="Data Sources Connected" />
      </div>
      <Card>
        <CardHeader><CardTitle className="text-base">Monthly Query Volume</CardTitle></CardHeader>
        <CardContent>
          <div className="flex items-end gap-3 h-40">
            {monthlyQueryTrend.map((m) => (
              <div key={m.month} className="flex-1 flex flex-col items-center gap-2">
                <span className="text-xs font-semibold">{m.queries}</span>
                <div
                  className="w-full bg-primary/20 hover:bg-primary/30 rounded-t transition-colors"
                  style={{ height: `${(m.queries / 400) * 120}px` }}
                />
                <span className="text-xs text-muted-foreground">{m.month}</span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- Pipeline Analytics (DASH-07) ----

function PipelineAnalytics() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Pipeline Analytics</h1>
        <p className="text-sm text-muted-foreground mt-1">Drug repurposing pipeline performance and success rates</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={GitBranch} value={274} label="Total Candidates" trend={{ value: 12 }} />
        <StatCard icon={Target} value={34} label="Phase II+" trend={{ value: 8 }} />
        <StatCard icon={Heart} value={72} label="Avg. Confidence Score" />
        <StatCard icon={Shield} value={89} label="Green Safety Tier" trend={{ value: 5 }} />
      </div>
      <Card>
        <CardHeader><CardTitle className="text-base">Pipeline by Phase</CardTitle></CardHeader>
        <CardContent>
          <div className="space-y-3">
            {[
              { phase: 'Discovery', count: 89, pct: 32 },
              { phase: 'Preclinical', count: 67, pct: 24 },
              { phase: 'Phase I', count: 45, pct: 16 },
              { phase: 'Phase II', count: 42, pct: 15 },
              { phase: 'Phase III', count: 20, pct: 7 },
              { phase: 'Approved', count: 11, pct: 4 },
            ].map((row) => (
              <div key={row.phase}>
                <div className="flex justify-between text-sm mb-1">
                  <span>{row.phase}</span>
                  <span className="font-medium">{row.count} ({row.pct}%)</span>
                </div>
                <Progress value={row.pct} className="h-2" />
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- Alert Center (DASH-10) ----

function AlertCenter() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Alert Center</h1>
        <p className="text-sm text-muted-foreground mt-1">Centralized alerts and notifications management</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard icon={Bell} value={4} label="Unread Alerts" />
        <StatCard icon={Activity} value={2} label="Critical Alerts" />
        <StatCard icon={Shield} value={0} label="Breach Alerts" />
      </div>
      <Card>
        <CardHeader><CardTitle className="text-base">All Notifications</CardTitle></CardHeader>
        <CardContent>
          <div className="space-y-2">
            {recentActivity.map((item) => (
              <div key={item.id} className="flex items-center gap-3 p-3 rounded-lg border border-border hover:bg-accent/50">
                <div className={cn(
                  'h-2 w-2 rounded-full shrink-0',
                  item.type === 'safety' ? 'bg-[#D4853A]' : 'bg-[#5B4FCF]'
                )} />
                <div className="flex-1">
                  <p className="text-sm"><span className="font-medium">{item.user}</span> {item.action} <span className="font-medium">{item.target}</span></p>
                  <p className="text-xs text-muted-foreground">{new Date(item.timestamp).toLocaleString()}</p>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function cn(...inputs: (string | false | undefined)[]) {
  return inputs.filter(Boolean).join(' ');
}

// ---- Generic Dashboard for other DASH screens ----

function GenericDashboard({ screenId }: { screenId: string }) {
  const meta = getScreenMeta(screenId);
  const iconMap: Record<string, React.ComponentType<{className?: string}>> = {
    DASH_05: FileText,
    DASH_06: Users,
    DASH_08: TrendingUp,
    DASH_09: Target,
    DASH_11: Heart,
    DASH_12: LineChart,
    DASH_13: Briefcase,
    DASH_14: Shield,
    DASH_15: Database,
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{meta?.name ?? screenId}</h1>
        <p className="text-sm text-muted-foreground mt-1">{meta?.description ?? ''}</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={BarChart3} value={128} label="Total Queries" trend={{ value: 8 }} />
        <StatCard icon={Activity} value={42} label="Active Items" />
        <StatCard icon={FileText} value={15} label="Reports" trend={{ value: 12 }} />
        <StatCard icon={Users} value={8} label="Team Members" />
      </div>
      <Card>
        <CardHeader><CardTitle className="text-base">Recent Activity</CardTitle></CardHeader>
        <CardContent>
          <div className="text-sm text-muted-foreground py-8 text-center">
            Detailed view for {meta?.name ?? screenId} will be available soon.
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
