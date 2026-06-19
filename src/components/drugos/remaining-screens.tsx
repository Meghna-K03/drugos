'use client';

import { useState, useMemo } from 'react';
import { useDrugOSNav } from './nav-context';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle, CardDescription, CardFooter } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Separator } from '@/components/ui/separator';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer, PieChart, Pie, Cell, LineChart, Line, AreaChart, Area, Legend } from 'recharts';
import { Search, Plus, Download, ChevronRight, ChevronDown, Check, X, AlertTriangle, Star, ExternalLink, Copy, Trash2, Edit, MoreHorizontal, Filter, ArrowRight, RefreshCw, Eye, Settings, Users, Shield, Key, Activity, TrendingUp, FileText, Clock, Zap, Globe, Lock, Bell, Mail, CreditCard, Database, Code, BookOpen, GitFork, Server, Building, User, Play, Send, HelpCircle, MessageSquare, BarChart3, Target, Award, Heart, LayoutDashboard, GitBranch, FolderKanban, Share2, Bookmark, Layers, Monitor, Smartphone, Calendar, DollarSign, Percent, Package, AlertCircle, CheckCircle2, XCircle, Info, ArrowUpRight, ArrowDownRight, ToggleLeft, ShieldCheck, Scale } from 'lucide-react';
import { motion } from 'framer-motion';
import { diseases, drugCandidates, clinicalTrials, users, auditLogs as mockAuditLogs, subscriptionPlans as mockPlans, billingHistory, apiKeys as mockApiKeys, webhooks, usageMetrics, dataSources, dealPipeline, organization, featureFlags, systemStatus, savedQueries, blogPosts, careers } from '@/lib/mock-data';
import { useProjects, useBillingPlans, useSubscription, useInvoices, useApiKeys, useNotifications, useAdminUsers, useAuditLogs } from './data-hooks';
import { api, errorMessage } from '@/lib/api-client';

const PRIMARY = '#5B4FCF';
const GREEN = '#1D9E75';
const ORANGE = '#D4853A';
const RED = '#C0392B';

function FadeIn({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  return <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3, delay }}>{children}</motion.div>;
}

function PageHeader({ title, desc, actions }: { title: string; desc?: string; actions?: React.ReactNode }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-6">
      <div><h1 className="text-2xl font-bold text-foreground">{title}</h1>{desc && <p className="text-sm text-muted-foreground mt-0.5">{desc}</p>}</div>
      {actions && <div className="flex items-center gap-2 flex-shrink-0">{actions}</div>}
    </div>
  );
}

function StatCard({ title, value, subtitle, icon: Icon, trend }: { title: string; value: string | number; subtitle?: string; icon?: React.ComponentType<{className?:string}>; trend?: string }) {
  return (
    <Card className="hover:shadow-md transition-shadow"><CardContent className="p-5"><div className="flex items-start justify-between"><div>
      <p className="text-sm text-muted-foreground">{title}</p><p className="text-2xl font-bold text-foreground mt-1">{value}</p>
      {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
      {trend && <p className={`text-xs mt-1 font-medium ${trend.startsWith('+') ? 'text-emerald-600' : 'text-red-500'}`}>{trend}</p>}
    </div>{Icon && <div className="h-10 w-10 rounded-lg bg-primary/10 flex items-center justify-center"><Icon className="h-5 w-5 text-primary" /></div>}</div></CardContent></Card>
  );
}

const CHART_COLORS = ['#5B4FCF', '#1D9E75', '#D4853A', '#C0392B', '#8B5CF6', '#06B6D4', '#EC4899', '#F59E0B'];

// ═══════════════════════════════════════════
// 1. PIPELINE SCREEN
// ═══════════════════════════════════════════
function PipelineScreen() {
  const { navigate } = useDrugOSNav();
  const [filter, setFilter] = useState('all');
  const stages = [
    { name: 'Discovery', count: 142, color: PRIMARY },
    { name: 'Preclinical', count: 48, color: '#8B5CF6' },
    { name: 'Phase I', count: 22, color: ORANGE },
    { name: 'Phase II', count: 14, color: '#06B6D4' },
    { name: 'Phase III', count: 6, color: GREEN },
    { name: 'Approved', count: 3, color: '#10B981' },
  ];
  const total = stages.reduce((s, x) => s + x.count, 0);
  const pipelineData = stages.map(s => ({ name: s.name, count: s.count, fill: s.color }));
  const pipelineItems = [
    { drug: 'Memantine', disease: "Huntington's", stage: 'Phase II', score: 87, safety: 'green' },
    { drug: 'Sirolimus', disease: 'ALS', stage: 'Phase I', score: 82, safety: 'green' },
    { drug: 'Metformin', disease: 'Glioblastoma', stage: 'Preclinical', score: 79, safety: 'yellow' },
    { drug: 'Dasatinib', disease: "Alzheimer's", stage: 'Discovery', score: 74, safety: 'yellow' },
    { drug: 'Naltrexone', disease: 'MS', stage: 'Phase III', score: 91, safety: 'green' },
    { drug: 'Ivermectin', disease: 'Breast Cancer', stage: 'Phase I', score: 68, safety: 'red' },
    { drug: 'Disulfiram', disease: 'Glioblastoma', stage: 'Phase II', score: 85, safety: 'yellow' },
    { drug: 'Propranolol', disease: 'Pancreatic', stage: 'Discovery', score: 62, safety: 'green' },
  ];
  const filtered = filter === 'all' ? pipelineItems : pipelineItems.filter(i => i.stage === filter);
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Repurposing Pipeline" desc="Track drug candidates through the repurposing pipeline" actions={<Button variant="outline" size="sm"><Download className="h-4 w-4 mr-1.5" />Export</Button>} />
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        {stages.map(s => (<Card key={s.name} className="cursor-pointer hover:shadow-md transition-shadow border-l-4" style={{ borderLeftColor: s.color }} onClick={() => setFilter(filter === s.name ? 'all' : s.name)}>
          <CardContent className="p-4"><p className="text-xs text-muted-foreground">{s.name}</p><p className="text-2xl font-bold mt-1">{s.count}</p><p className="text-xs text-muted-foreground">{Math.round(s.count/total*100)}%</p></CardContent>
        </Card>))}
      </div>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Pipeline Funnel</CardTitle></CardHeader>
        <CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><BarChart data={pipelineData} layout="vertical"><CartesianGrid strokeDasharray="3 3" /><XAxis type="number" /><YAxis dataKey="name" type="category" width={90} /><RechartsTooltip /><Bar dataKey="count" radius={[0, 4, 4, 0]}>{pipelineData.map((entry, i) => <Cell key={i} fill={entry.fill} />)}</Bar></BarChart></ResponsiveContainer></div></CardContent>
      </Card>
      <div className="flex items-center gap-2 flex-wrap mb-2"><Badge variant={filter === 'all' ? 'default' : 'outline'} className="cursor-pointer" onClick={() => setFilter('all')}>All</Badge>{stages.map(s => <Badge key={s.name} variant={filter === s.name ? 'default' : 'outline'} className="cursor-pointer" onClick={() => setFilter(s.name)}>{s.name} ({s.count})</Badge>)}</div>
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Drug</TableHead><TableHead>Disease</TableHead><TableHead>Stage</TableHead><TableHead>Score</TableHead><TableHead>Safety</TableHead></TableRow></TableHeader>
        <TableBody>{filtered.map((item, i) => (<TableRow key={i} className="cursor-pointer hover:bg-muted/30"><TableCell className="font-medium">{item.drug}</TableCell><TableCell>{item.disease}</TableCell>
          <TableCell><Badge variant="outline">{item.stage}</Badge></TableCell><TableCell><span className="font-bold" style={{ color: item.score >= 80 ? GREEN : item.score >= 60 ? ORANGE : RED }}>{item.score}</span></TableCell>
          <TableCell><Badge variant={item.safety === 'green' ? 'default' : item.safety === 'yellow' ? 'secondary' : 'destructive'} className="text-xs">{item.safety === 'green' ? 'Safe' : item.safety === 'yellow' ? 'Caution' : 'Risk'}</Badge></TableCell>
        </TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 2. ANALYTICS SCREEN
// ═══════════════════════════════════════════
function AnalyticsScreen() {
  const [timeRange, setTimeRange] = useState('6m');
  const queryData = [{ month: 'Jan', queries: 180, api: 22000 },{ month: 'Feb', queries: 220, api: 28000 },{ month: 'Mar', queries: 290, api: 35000 },{ month: 'Apr', queries: 310, api: 38000 },{ month: 'May', queries: 340, api: 42000 },{ month: 'Jun', queries: 342, api: 45230 }];
  const topDiseases = [{ name: "Huntington's", queries: 342, growth: '+24%' },{ name: "Alzheimer's", queries: 289, growth: '+18%' },{ name: 'Glioblastoma', queries: 234, growth: '+31%' },{ name: 'ALS', queries: 198, growth: '+12%' },{ name: 'MS', queries: 167, growth: '+8%' }];
  const successData = [{ name: 'Discovery', value: 142 },{ name: 'Preclinical', value: 48 },{ name: 'Clinical', value: 42 },{ name: 'Approved', value: 3 }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Analytics" desc="Platform usage and performance metrics" actions={<Select value={timeRange} onValueChange={setTimeRange}><SelectTrigger className="w-32"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="1m">1 Month</SelectItem><SelectItem value="3m">3 Months</SelectItem><SelectItem value="6m">6 Months</SelectItem><SelectItem value="1y">1 Year</SelectItem></SelectContent></Select>} />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard title="Total Queries" value="1,682" icon={Search} trend="+24%" />
        <StatCard title="API Calls" value="210,230" icon={Code} trend="+18%" />
        <StatCard title="Candidates Found" value="2,345" icon={Target} trend="+31%" />
        <StatCard title="Avg Score" value="73.4" icon={BarChart3} trend="+5%" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Query Volume</CardTitle></CardHeader><CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><AreaChart data={queryData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="month" /><YAxis /><RechartsTooltip /><Area type="monotone" dataKey="queries" stroke={PRIMARY} fill={`${PRIMARY}20`} /></AreaChart></ResponsiveContainer></div></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Pipeline Distribution</CardTitle></CardHeader><CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={successData} cx="50%" cy="50%" innerRadius={60} outerRadius={90} paddingAngle={4} dataKey="value">{successData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i]} />)}</Pie><RechartsTooltip /><Legend /></PieChart></ResponsiveContainer></div></CardContent></Card>
      </div>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Top Searched Diseases</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Disease</TableHead><TableHead>Queries</TableHead><TableHead>Growth</TableHead></TableRow></TableHeader>
        <TableBody>{topDiseases.map(d => (<TableRow key={d.name}><TableCell className="font-medium">{d.name}</TableCell><TableCell>{d.queries}</TableCell><TableCell><span className="text-emerald-600 font-medium">{d.growth}</span></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 3. TEAM MEMBERS SCREEN
// ═══════════════════════════════════════════
function TeamMembersScreen() {
  const { navigate } = useDrugOSNav();
  const [search, setSearch] = useState('');
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('viewer');
  const teamMembers = [
    { name: 'Dr. Sarah Chen', email: 'sarah@pharma.com', role: 'Admin', status: 'active', lastActive: '2 min ago', avatar: 'SC' },
    { name: 'James Wilson', email: 'james@pharma.com', role: 'Researcher', status: 'active', lastActive: '15 min ago', avatar: 'JW' },
    { name: 'Dr. Priya Patel', email: 'priya@university.edu', role: 'Researcher', status: 'active', lastActive: '1 hr ago', avatar: 'PP' },
    { name: 'Mike Rodriguez', email: 'mike@pharma.com', role: 'Viewer', status: 'invited', lastActive: 'Pending', avatar: 'MR' },
    { name: 'Dr. Lisa Kim', email: 'lisa@pharma.com', role: 'Researcher', status: 'active', lastActive: '3 hrs ago', avatar: 'LK' },
    { name: 'Tom Baker', email: 'tom@partner.org', role: 'CRO Partner', status: 'active', lastActive: '1 day ago', avatar: 'TB' },
  ];
  const filtered = teamMembers.filter(m => m.name.toLowerCase().includes(search.toLowerCase()) || m.email.toLowerCase().includes(search.toLowerCase()));
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Team Members" desc={`${teamMembers.length} members in your organization`} actions={<><div className="relative"><Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" /><Input placeholder="Search members..." value={search} onChange={e => setSearch(e.target.value)} className="pl-9 w-56" /></div><Button style={{ backgroundColor: PRIMARY }} onClick={() => setInviteOpen(true)}><Plus className="h-4 w-4 mr-1.5" />Invite Member</Button></>} />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Member</TableHead><TableHead>Role</TableHead><TableHead>Status</TableHead><TableHead>Last Active</TableHead><TableHead></TableHead></TableRow></TableHeader>
        <TableBody>{filtered.map(m => (<TableRow key={m.email}><TableCell><div className="flex items-center gap-3"><Avatar className="h-8 w-8"><AvatarFallback className="bg-primary/10 text-primary text-xs">{m.avatar}</AvatarFallback></Avatar><div><p className="font-medium text-sm">{m.name}</p><p className="text-xs text-muted-foreground">{m.email}</p></div></div></TableCell>
          <TableCell><Select defaultValue={m.role.toLowerCase()}><SelectTrigger className="w-32 h-7 text-xs"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="admin">Admin</SelectItem><SelectItem value="researcher">Researcher</SelectItem><SelectItem value="viewer">Viewer</SelectItem><SelectItem value="cro partner">CRO Partner</SelectItem></SelectContent></Select></TableCell>
          <TableCell><Badge variant={m.status === 'active' ? 'default' : 'outline'}>{m.status}</Badge></TableCell>
          <TableCell className="text-sm text-muted-foreground">{m.lastActive}</TableCell>
          <TableCell><Button variant="ghost" size="sm" className="h-7 w-7 p-0"><MoreHorizontal className="h-4 w-4" /></Button></TableCell>
        </TableRow>))}</TableBody></Table></CardContent></Card>
      <Dialog open={inviteOpen} onOpenChange={setInviteOpen}><DialogContent><DialogHeader><DialogTitle>Invite Team Member</DialogTitle><DialogDescription>Send an invitation to join your DrugOS workspace</DialogDescription></DialogHeader>
        <div className="space-y-4"><div><Label>Email Address</Label><Input placeholder="colleague@company.com" value={inviteEmail} onChange={e => setInviteEmail(e.target.value)} /></div>
        <div><Label>Role</Label><Select value={inviteRole} onValueChange={setInviteRole}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="admin">Admin</SelectItem><SelectItem value="researcher">Researcher</SelectItem><SelectItem value="viewer">Viewer</SelectItem></SelectContent></Select></div></div>
        <DialogFooter><Button variant="outline" onClick={() => setInviteOpen(false)}>Cancel</Button><Button style={{ backgroundColor: PRIMARY }} onClick={() => setInviteOpen(false)}>Send Invitation</Button></DialogFooter>
      </DialogContent></Dialog>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 4. PROJECTS SCREEN
// ═══════════════════════════════════════════
function ProjectsScreen() {
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [creating, setCreating] = useState(false);
  const [createErr, setCreateErr] = useState<string | null>(null);
  const { data, loading, error, refetch } = useProjects();
  const projects = data?.items || [];

  const handleCreate = async () => {
    if (!newName.trim()) { setCreateErr('Name is required'); return; }
    setCreating(true); setCreateErr(null);
    try {
      await api.projects.create({ name: newName, description: newDesc || undefined, visibility: 'private' });
      setCreateOpen(false); setNewName(''); setNewDesc('');
      await refetch();
    } catch (e) {
      setCreateErr(errorMessage(e));
    } finally { setCreating(false); }
  };

  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Projects" desc={`${projects.length} research projects`} actions={<Button style={{ backgroundColor: PRIMARY }} onClick={() => setCreateOpen(true)}><Plus className="h-4 w-4 mr-1.5" />New Project</Button>} />
      {loading && <Card><CardContent className="p-8 text-center text-muted-foreground">Loading projects…</CardContent></Card>}
      {error && <Card><CardContent className="p-8 text-center text-red-600">Failed to load: {error}</CardContent></Card>}
      {!loading && !error && projects.length === 0 && <Card><CardContent className="p-8 text-center text-muted-foreground">No projects yet — click “New Project” to create one.</CardContent></Card>}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {projects.map((p: any) => (<Card key={p.id} className="hover:shadow-md transition-shadow cursor-pointer"><CardContent className="p-5"><div className="flex items-start justify-between mb-3"><div><h3 className="font-semibold text-sm">{p.name}</h3><p className="text-xs text-muted-foreground mt-1">{p.description || 'No description'}</p></div><Badge variant={p.status === 'active' ? 'default' : 'secondary'}>{p.status}</Badge></div>
          <div className="flex items-center justify-between text-xs text-muted-foreground"><div className="flex items-center gap-3"><span className="flex items-center gap-1"><Target className="h-3 w-3" />{(p._count?.hypotheses || 0)} hypotheses</span><span className="flex items-center gap-1"><MessageSquare className="h-3 w-3" />{(p._count?.comments || 0)} comments</span></div><span>Updated {new Date(p.updatedAt).toLocaleDateString()}</span></div>
        </CardContent></Card>))}
      </div>
      <Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogContent><DialogHeader><DialogTitle>Create New Project</DialogTitle><DialogDescription>Set up a new research project workspace</DialogDescription></DialogHeader>
        <div className="space-y-4"><div><Label>Project Name</Label><Input placeholder="e.g. Parkinson's Repurposing" value={newName} onChange={e => setNewName(e.target.value)} /></div><div><Label>Description</Label><Textarea placeholder="Describe the research goal..." value={newDesc} onChange={e => setNewDesc(e.target.value)} /></div>
        {createErr && <p className="text-sm text-red-600">{createErr}</p>}
        </div>
        <DialogFooter><Button variant="outline" onClick={() => setCreateOpen(false)}>Cancel</Button><Button style={{ backgroundColor: PRIMARY }} onClick={handleCreate} disabled={creating}>{creating ? 'Creating…' : 'Create Project'}</Button></DialogFooter>
      </DialogContent></Dialog>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 5. SHARED QUERIES SCREEN
// ═══════════════════════════════════════════
function SharedQueriesScreen() {
  const sharedQueries = [
    { name: "Huntington's Top 10", sharedBy: 'Dr. Sarah Chen', date: '2 hours ago', candidates: 10, topScore: 87 },
    { name: 'Rare Disease Panel v2', sharedBy: 'James Wilson', date: '1 day ago', candidates: 24, topScore: 82 },
    { name: 'Oncology Cross-Filter', sharedBy: 'Dr. Priya Patel', date: '3 days ago', candidates: 45, topScore: 79 },
    { name: 'ALS Candidates Filtered', sharedBy: 'Dr. Lisa Kim', date: '1 week ago', candidates: 8, topScore: 91 },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Shared Queries" desc="Queries shared by your team members" />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Query Name</TableHead><TableHead>Shared By</TableHead><TableHead>Date</TableHead><TableHead>Candidates</TableHead><TableHead>Top Score</TableHead><TableHead></TableHead></TableRow></TableHeader>
        <TableBody>{sharedQueries.map(q => (<TableRow key={q.name}><TableCell className="font-medium">{q.name}</TableCell><TableCell>{q.sharedBy}</TableCell><TableCell className="text-muted-foreground">{q.date}</TableCell><TableCell>{q.candidates}</TableCell>
          <TableCell><span className="font-bold" style={{ color: q.topScore >= 80 ? GREEN : ORANGE }}>{q.topScore}</span></TableCell>
          <TableCell><Button variant="outline" size="sm"><Copy className="h-3 w-3 mr-1" />Copy to My Queries</Button></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 6. ANNOTATIONS SCREEN
// ═══════════════════════════════════════════
function AnnotationsScreen() {
  const [newComment, setNewComment] = useState('');
  const annotations = [
    { candidate: 'Memantine', disease: "Huntington's", author: 'Dr. Sarah Chen', comment: 'Strong KG evidence. NMDA receptor modulation is well-documented. Consider off-target profiling for cardiac effects.', date: '2 hours ago', resolved: false },
    { candidate: 'Sirolimus', disease: 'ALS', author: 'James Wilson', comment: 'mTOR pathway inhibition shows promise. Need to check for immunosuppression contraindications.', date: '1 day ago', resolved: false },
    { candidate: 'Metformin', disease: 'Glioblastoma', author: 'Dr. Priya Patel', comment: 'AMPK activation mechanism is solid. Preclinical data from 3 independent labs.', date: '3 days ago', resolved: true },
    { candidate: 'Dasatinib', disease: "Alzheimer's", author: 'Dr. Lisa Kim', comment: 'Src family kinase inhibition is novel for AD. Monitor for pleural effusion risk.', date: '1 week ago', resolved: false },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Annotations" desc="Collaborative notes on drug candidates" actions={<Badge variant="outline">{annotations.filter(a => !a.resolved).length} Open</Badge>} />
      <div className="space-y-4">
        {annotations.map((a, i) => (<Card key={i} className={a.resolved ? 'opacity-60' : ''}><CardContent className="p-4"><div className="flex items-start justify-between mb-2"><div className="flex items-center gap-2"><Badge variant="secondary" className="text-xs">{a.candidate}</Badge><Badge variant="outline" className="text-xs">{a.disease}</Badge>{a.resolved && <Badge className="text-xs bg-green-100 text-green-700">Resolved</Badge>}</div><Button variant="ghost" size="sm">{a.resolved ? 'Reopen' : 'Resolve'}</Button></div>
          <p className="text-sm">{a.comment}</p><div className="flex items-center gap-2 mt-3 text-xs text-muted-foreground"><span>{a.author}</span><span>·</span><span>{a.date}</span></div>
        </CardContent></Card>))}
      </div>
      <Card><CardContent className="p-4"><div className="flex gap-3"><Avatar className="h-8 w-8"><AvatarFallback className="bg-primary/10 text-primary text-xs">YO</AvatarFallback></Avatar><div className="flex-1"><Textarea placeholder="Add a comment or annotation..." value={newComment} onChange={e => setNewComment(e.target.value)} className="min-h-[60px]" /><div className="flex justify-end mt-2"><Button size="sm" style={{ backgroundColor: PRIMARY }}>Post Comment</Button></div></div></div></CardContent></Card>
    </div></FadeIn>
  );
}


// ═══════════════════════════════════════════
// 7. DATA SOURCES SCREEN
// ═══════════════════════════════════════════
function DataSourcesScreen() {
  const [syncing, setSyncing] = useState<string | null>(null);
  const sources = [
    { name: 'DrugBank', records: '13,481 drugs', lastSync: '2 hours ago', status: 'synced', icon: '💊' },
    { name: 'ChEMBL', records: '2.1M compounds', lastSync: '4 hours ago', status: 'synced', icon: '🧪' },
    { name: 'OpenTargets', records: '19,524 targets', lastSync: '6 hours ago', status: 'synced', icon: '🎯' },
    { name: 'ClinicalTrials.gov', records: '430K trials', lastSync: '1 day ago', status: 'synced', icon: '🏥' },
    { name: 'UniProt', records: '570K proteins', lastSync: '1 day ago', status: 'synced', icon: '🧬' },
    { name: 'PubMed', records: '36M articles', lastSync: '3 hours ago', status: 'synced', icon: '📚' },
    { name: 'KEGG Pathways', records: '580 pathways', lastSync: '1 week ago', status: 'stale', icon: '🔗' },
    { name: 'Orphanet', records: '6,187 diseases', lastSync: '2 days ago', status: 'synced', icon: '❤️' },
  ];
  const handleSync = (name: string) => { setSyncing(name); setTimeout(() => setSyncing(null), 2000); };
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Data Sources" desc={`${sources.length} connected data sources`} actions={<Button style={{ backgroundColor: PRIMARY }}><Plus className="h-4 w-4 mr-1.5" />Add Source</Button>} />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {sources.map(s => (<Card key={s.name} className="hover:shadow-md transition-shadow"><CardContent className="p-5"><div className="flex items-start justify-between mb-3"><div className="flex items-center gap-3"><span className="text-2xl">{s.icon}</span><div><h3 className="font-semibold text-sm">{s.name}</h3><p className="text-xs text-muted-foreground">{s.records}</p></div></div><Badge variant={s.status === 'synced' ? 'default' : 'secondary'}>{s.status}</Badge></div>
          <div className="flex items-center justify-between"><span className="text-xs text-muted-foreground">Last sync: {s.lastSync}</span><Button variant="outline" size="sm" onClick={() => handleSync(s.name)} disabled={syncing === s.name}>{syncing === s.name ? <><RefreshCw className="h-3 w-3 mr-1 animate-spin" />Syncing</> : <><RefreshCw className="h-3 w-3 mr-1" />Sync</>}</Button></div>
        </CardContent></Card>))}
      </div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 8. GRAPH STATISTICS SCREEN
// ═══════════════════════════════════════════
function GraphStatisticsScreen() {
  const nodeTypes = [{ type: 'Drug', count: 13481, color: PRIMARY },{ type: 'Disease', count: 7243, color: GREEN },{ type: 'Gene', count: 19524, color: ORANGE },{ type: 'Pathway', count: 580, color: RED },{ type: 'Protein', count: 570321, color: '#8B5CF6' }];
  const edgeTypes = [{ type: 'treats', count: 84200 },{ type: 'targets', count: 195400 },{ type: 'interacts', count: 2.1 },{ type: 'associated', count: 62000 },{ type: 'expressed', count: 340000 }];
  const growthData = [{ month: 'Jan', nodes: 480000, edges: 3200000 },{ month: 'Feb', nodes: 490000, edges: 3350000 },{ month: 'Mar', nodes: 510000, edges: 3500000 },{ month: 'Apr', nodes: 530000, edges: 3700000 },{ month: 'May', nodes: 558000, edges: 3900000 },{ month: 'Jun', nodes: 611000, edges: 4200000 }];
  const totalNodes = nodeTypes.reduce((s, n) => s + n.count, 0);
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Knowledge Graph Statistics" desc={`${totalNodes.toLocaleString()} total nodes across 5 entity types`} />
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
        {nodeTypes.map(n => (<Card key={n.type}><CardContent className="p-4"><div className="flex items-center gap-2 mb-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: n.color }} /><span className="text-xs font-medium text-muted-foreground">{n.type}</span></div><p className="text-xl font-bold">{n.count.toLocaleString()}</p></CardContent></Card>))}
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Node Distribution</CardTitle></CardHeader><CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={nodeTypes.map(n => ({ name: n.type, value: n.count }))} cx="50%" cy="50%" innerRadius={50} outerRadius={80} paddingAngle={3} dataKey="value">{nodeTypes.map((n, i) => <Cell key={i} fill={n.color} />)}</Pie><RechartsTooltip /><Legend /></PieChart></ResponsiveContainer></div></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Graph Growth</CardTitle></CardHeader><CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><AreaChart data={growthData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="month" /><YAxis /><RechartsTooltip /><Area type="monotone" dataKey="nodes" stroke={PRIMARY} fill={`${PRIMARY}20`} /></AreaChart></ResponsiveContainer></div></CardContent></Card>
      </div>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Edge Types</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Edge Type</TableHead><TableHead>Count</TableHead></TableRow></TableHeader>
        <TableBody>{edgeTypes.map(e => (<TableRow key={e.type}><TableCell className="font-medium capitalize">{e.type}</TableCell><TableCell>{typeof e.count === 'number' && e.count > 1000 ? e.count.toLocaleString() : e.count + 'M'}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 9. QUALITY SCREEN
// ═══════════════════════════════════════════
function QualityScreen() {
  const qualityMetrics = [{ source: 'DrugBank', completeness: 96, freshness: 98, duplicates: 2, reliability: 97 },{ source: 'ChEMBL', completeness: 91, freshness: 94, duplicates: 5, reliability: 95 },{ source: 'OpenTargets', completeness: 88, freshness: 92, duplicates: 8, reliability: 90 },{ source: 'ClinicalTrials.gov', completeness: 94, freshness: 96, duplicates: 3, reliability: 98 },{ source: 'UniProt', completeness: 97, freshness: 95, duplicates: 1, reliability: 99 }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Data Quality" desc="Monitor and improve data quality across all sources" />
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <StatCard title="Avg Completeness" value="93.2%" icon={CheckCircle2} />
        <StatCard title="Avg Freshness" value="95.0%" icon={RefreshCw} />
        <StatCard title="Duplicates" value="19" icon={Copy} />
        <StatCard title="Reliability" value="95.8%" icon={ShieldCheck} />
      </div>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Source Quality Matrix</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Source</TableHead><TableHead>Completeness</TableHead><TableHead>Freshness</TableHead><TableHead>Duplicates</TableHead><TableHead>Reliability</TableHead></TableRow></TableHeader>
        <TableBody>{qualityMetrics.map(q => (<TableRow key={q.source}><TableCell className="font-medium">{q.source}</TableCell>
          <TableCell><div className="flex items-center gap-2"><Progress value={q.completeness} className="w-20 h-2" /><span className="text-xs">{q.completeness}%</span></div></TableCell>
          <TableCell><div className="flex items-center gap-2"><Progress value={q.freshness} className="w-20 h-2" /><span className="text-xs">{q.freshness}%</span></div></TableCell>
          <TableCell><Badge variant={q.duplicates > 5 ? 'destructive' : q.duplicates > 3 ? 'secondary' : 'outline'}>{q.duplicates}</Badge></TableCell>
          <TableCell><div className="flex items-center gap-2"><Progress value={q.reliability} className="w-20 h-2" /><span className="text-xs">{q.reliability}%</span></div></TableCell>
        </TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 10. SUBSCRIPTION SCREEN
// ═══════════════════════════════════════════
function SubscriptionScreen() {
  const { navigate } = useDrugOSNav();
  const { data: subData, loading: subLoading } = useSubscription();
  const { data: plansData, loading: plansLoading } = useBillingPlans();
  const [changing, setChanging] = useState<string | null>(null);
  const [changeErr, setChangeErr] = useState<string | null>(null);

  const sub = subData?.subscription;
  const plans = plansData?.plans || [];
  const currentPlanObj = plans.find((p: any) => p.id === sub?.plan);
  const usageBars = [
    { label: 'Seats', used: sub?.seats || 0, limit: currentPlanObj?.seats || 1 },
    { label: 'Plan', used: 1, limit: 1 },
  ];

  const handleChange = async (planId: string) => {
    setChanging(planId); setChangeErr(null);
    try {
      await api.billing.changePlan(planId);
      window.location.reload();
    } catch (e) {
      setChangeErr(errorMessage(e));
    } finally { setChanging(null); }
  };

  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Subscription" desc="Manage your plan and billing" />
      {changeErr && <Card><CardContent className="p-3 text-sm text-red-600">{changeErr}</CardContent></Card>}
      <Card className="border-primary/30"><CardContent className="p-6">
        {subLoading ? <p className="text-muted-foreground">Loading…</p> : (
          <>
            <div className="flex items-center justify-between mb-4"><div><h3 className="text-lg font-semibold capitalize">{sub?.plan || 'free'} Plan</h3><p className="text-sm text-muted-foreground">Status: {sub?.status || '—'} · Current period ends {sub?.currentPeriodEnd ? new Date(sub.currentPeriodEnd).toLocaleDateString() : '—'}</p></div><div className="text-right"><p className="text-3xl font-bold">${((currentPlanObj?.priceCents || 0) / 100).toFixed(2)}<span className="text-sm text-muted-foreground">/mo</span></p></div></div>
            <div className="space-y-3">{usageBars.map(u => (<div key={u.label}><div className="flex justify-between text-sm mb-1"><span className="text-muted-foreground">{u.label}</span><span className="font-medium">{u.used} / {u.limit}</span></div><Progress value={u.limit > 0 ? (u.used / u.limit) * 100 : 0} className="h-2" /></div>))}</div>
          </>
        )}
      </CardContent></Card>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {plansLoading ? <p className="text-muted-foreground">Loading plans…</p> : plans.filter((p: any) => p.id !== sub?.plan).slice(0, 3).map((plan: any) => (
          <Card key={plan.id} className="hover:shadow-md transition-shadow"><CardHeader><CardTitle className="text-lg">{plan.name}</CardTitle><div className="mt-1"><span className="text-2xl font-bold">${(plan.priceCents / 100).toFixed(2)}</span><span className="text-sm text-muted-foreground">/mo</span></div></CardHeader>
            <CardContent><ul className="space-y-1.5">{plan.features.slice(0, 4).map((f: string, i: number) => <li key={i} className="flex items-center gap-2 text-sm"><Check className="h-3 w-3 text-green-500" />{f}</li>)}</ul></CardContent>
            <CardFooter><Button variant="outline" className="w-full" onClick={() => handleChange(plan.id)} disabled={changing === plan.id}>{changing === plan.id ? 'Changing…' : plan.priceCents === 0 ? 'Downgrade' : 'Upgrade'}</Button></CardFooter></Card>
        ))}
      </div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 11. USAGE SCREEN
// ═══════════════════════════════════════════
function UsageScreen() {
  const usageData = [{ day: 'Mon', queries: 45, api: 6800 },{ day: 'Tue', queries: 52, api: 7200 },{ day: 'Wed', queries: 38, api: 5400 },{ day: 'Thu', queries: 61, api: 8900 },{ day: 'Fri', queries: 55, api: 7600 },{ day: 'Sat', queries: 22, api: 3200 },{ day: 'Sun', queries: 18, api: 2800 }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Usage" desc="Monitor your platform usage and limits" />
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <StatCard title="Queries This Month" value="342/1,000" icon={Search} /><StatCard title="API Calls Today" value="4,523" icon={Code} trend="+12%" /><StatCard title="Storage Used" value="2.4 GB" icon={Database} /><StatCard title="Team Seats" value="8/25" icon={Users} />
      </div>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Usage Trend (This Week)</CardTitle></CardHeader><CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><BarChart data={usageData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="day" /><YAxis /><RechartsTooltip /><Bar dataKey="queries" fill={PRIMARY} radius={[4, 4, 0, 0]} /></BarChart></ResponsiveContainer></div></CardContent></Card>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">API Calls Trend</CardTitle></CardHeader><CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><AreaChart data={usageData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="day" /><YAxis /><RechartsTooltip /><Area type="monotone" dataKey="api" stroke={GREEN} fill={`${GREEN}20`} /></AreaChart></ResponsiveContainer></div></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 12. DEALS SCREEN
// ═══════════════════════════════════════════
function DealsScreen() {
  const deals = [
    { drug: 'Memantine', disease: "Huntington's", licensee: 'NeuroPharm Inc', stage: 'Term Sheet', value: '$2.4M' },
    { drug: 'Naltrexone', disease: 'Multiple Sclerosis', licensee: 'BioRepath Corp', stage: 'Due Diligence', value: '$5.1M' },
    { drug: 'Sirolimus', disease: 'ALS', licensee: 'MotorNeuron Therapies', stage: 'LOI Signed', value: '$3.8M' },
    { drug: 'Metformin', disease: 'Glioblastoma', licensee: 'Oncore Corp', stage: 'Negotiation', value: '$8.2M' },
  ];
  const stageColors: Record<string, string> = { 'LOI Signed': GREEN, 'Due Diligence': ORANGE, 'Term Sheet': PRIMARY, 'Negotiation': '#8B5CF6' };
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Discovery Deals" desc="Manage licensing deals for repurposing candidates" actions={<Button style={{ backgroundColor: PRIMARY }}><Plus className="h-4 w-4 mr-1.5" />New Deal</Button>} />
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <StatCard title="Active Deals" value={deals.length} icon={DollarSign} /><StatCard title="Pipeline Value" value="$19.5M" icon={TrendingUp} /><StatCard title="Avg Deal Size" value="$4.9M" icon={BarChart3} /><StatCard title="Close Rate" value="68%" icon={Target} />
      </div>
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Drug</TableHead><TableHead>Disease</TableHead><TableHead>Licensee</TableHead><TableHead>Stage</TableHead><TableHead>Value</TableHead></TableRow></TableHeader>
        <TableBody>{deals.map(d => (<TableRow key={d.drug + d.disease}><TableCell className="font-medium">{d.drug}</TableCell><TableCell>{d.disease}</TableCell><TableCell>{d.licensee}</TableCell>
          <TableCell><Badge style={{ backgroundColor: `${stageColors[d.stage]}15`, color: stageColors[d.stage], borderColor: `${stageColors[d.stage]}30` }} variant="outline">{d.stage}</Badge></TableCell>
          <TableCell className="font-semibold">{d.value}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 13. INVOICES SCREEN
// ═══════════════════════════════════════════
function InvoicesScreen() {
  const { data, loading, error } = useInvoices();
  const invoices = data?.items || [];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Invoices" desc="Billing history and invoice management" />
      {loading && <Card><CardContent className="p-8 text-center text-muted-foreground">Loading invoices…</CardContent></Card>}
      {error && <Card><CardContent className="p-8 text-center text-red-600">{error}</CardContent></Card>}
      {!loading && !error && invoices.length === 0 && <Card><CardContent className="p-8 text-center text-muted-foreground">No invoices yet — invoices appear here once you upgrade to a paid plan.</CardContent></Card>}
      {invoices.length > 0 && <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Invoice</TableHead><TableHead>Date</TableHead><TableHead>Amount</TableHead><TableHead>Status</TableHead><TableHead>Due</TableHead></TableRow></TableHeader>
        <TableBody>{invoices.map((inv: any) => (<TableRow key={inv.id}><TableCell className="font-mono text-sm">{inv.number}</TableCell><TableCell>{inv.createdAt ? new Date(inv.createdAt).toLocaleDateString() : '—'}</TableCell><TableCell className="font-semibold">${(inv.amountCents / 100).toFixed(2)} {inv.currency}</TableCell>
          <TableCell><Badge variant={inv.status === 'paid' ? 'default' : 'secondary'}>{inv.status}</Badge></TableCell><TableCell>{inv.dueDate ? new Date(inv.dueDate).toLocaleDateString() : '—'}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>}
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 14. USERS ADMIN SCREEN
// ═══════════════════════════════════════════
function UsersAdminScreen() {
  const [search, setSearch] = useState('');
  const [inviteOpen, setInviteOpen] = useState(false);
  const { data, loading, error } = useAdminUsers();
  const adminUsers = data?.items || [];
  const filtered = adminUsers.filter((u: any) => (u.name || '').toLowerCase().includes(search.toLowerCase()) || (u.email || '').toLowerCase().includes(search.toLowerCase()));
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="User Management" desc={`${adminUsers.length} users across all organizations`} actions={<><div className="relative"><Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" /><Input placeholder="Search users..." value={search} onChange={e => setSearch(e.target.value)} className="pl-9 w-56" /></div><Button style={{ backgroundColor: PRIMARY }} onClick={() => setInviteOpen(true)}><Plus className="h-4 w-4 mr-1.5" />Add User</Button></>} />
      {loading && <Card><CardContent className="p-8 text-center text-muted-foreground">Loading users…</CardContent></Card>}
      {error && <Card><CardContent className="p-8 text-center text-red-600">{error}</CardContent></Card>}
      {!loading && !error && filtered.length > 0 && <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>User</TableHead><TableHead>Role</TableHead><TableHead>Status</TableHead><TableHead>Last Active</TableHead><TableHead></TableHead></TableRow></TableHeader>
        <TableBody>{filtered.map((u: any) => (<TableRow key={u.id}><TableCell><div className="flex items-center gap-3"><Avatar className="h-8 w-8"><AvatarFallback className="bg-primary/10 text-primary text-xs">{(u.name || u.email || '?').split(' ').map((n: string) => n[0]).join('').toUpperCase().slice(0, 2)}</AvatarFallback></Avatar><div><p className="font-medium text-sm">{u.name || '(no name)'}</p><p className="text-xs text-muted-foreground">{u.email}</p></div></div></TableCell>
          <TableCell><Badge variant="outline">{u.role}</Badge></TableCell><TableCell><Badge variant={u.status === 'active' ? 'default' : u.status === 'suspended' ? 'destructive' : 'secondary'}>{u.status}</Badge></TableCell>
          <TableCell className="text-sm text-muted-foreground">{u.lastLoginAt ? new Date(u.lastLoginAt).toLocaleString() : 'Never'}</TableCell><TableCell><Button variant="ghost" size="sm" className="h-7 w-7 p-0"><MoreHorizontal className="h-4 w-4" /></Button></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>}
      <Dialog open={inviteOpen} onOpenChange={setInviteOpen}><DialogContent><DialogHeader><DialogTitle>Add New User</DialogTitle><DialogDescription>User invitations require admin API support. For now, ask the new user to register at /auth/register.</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={() => setInviteOpen(false)}>Close</Button></DialogFooter></DialogContent></Dialog>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 15. ROLES SCREEN
// ═══════════════════════════════════════════
function RolesScreen() {
  const roles = [
    { name: 'Admin', desc: 'Full platform access', users: 2, perms: ['All'] },
    { name: 'Researcher', desc: 'Search, analyze, and export', users: 5, perms: ['Search', 'Analyze', 'Export', 'Collaborate'] },
    { name: 'Viewer', desc: 'Read-only access', users: 3, perms: ['View', 'Export'] },
    { name: 'CRO Partner', desc: 'External collaborator', users: 1, perms: ['View', 'Analyze', 'Collaborate'] },
    { name: 'Academic', desc: 'Academic researcher', users: 4, perms: ['Search', 'Analyze', 'Export'] },
  ];
  const allPerms = ['Search', 'Analyze', 'Export', 'Collaborate', 'View', 'Admin', 'Billing'];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Roles & Permissions" desc="Manage role-based access control" actions={<Button style={{ backgroundColor: PRIMARY }}><Plus className="h-4 w-4 mr-1.5" />Create Role</Button>} />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Role</TableHead><TableHead>Description</TableHead><TableHead>Users</TableHead>{allPerms.map(p => <TableHead key={p} className="text-center text-xs">{p}</TableHead>)}</TableRow></TableHeader>
        <TableBody>{roles.map(r => (<TableRow key={r.name}><TableCell className="font-medium">{r.name}</TableCell><TableCell className="text-sm text-muted-foreground">{r.desc}</TableCell><TableCell>{r.users}</TableCell>
          {allPerms.map(p => <TableCell key={p} className="text-center">{r.perms.includes('All') || r.perms.includes(p) ? <Check className="h-4 w-4 text-green-500 mx-auto" /> : <X className="h-4 w-4 text-muted-foreground/30 mx-auto" />}</TableCell>)}</TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 16. SSO SCREEN
// ═══════════════════════════════════════════
function SSOScreen() {
  const [enabled, setEnabled] = useState(false);
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Single Sign-On (SSO)" desc="Configure SAML or OIDC identity provider" />
      <Card><CardContent className="p-6 space-y-6">
        <div className="flex items-center justify-between"><div><h3 className="font-semibold">Enable SSO</h3><p className="text-sm text-muted-foreground">Allow team members to sign in via your identity provider</p></div><Switch checked={enabled} onCheckedChange={setEnabled} /></div>
        <Separator />
        <Tabs defaultValue="saml"><TabsList><TabsTrigger value="saml">SAML 2.0</TabsTrigger><TabsTrigger value="oidc">OIDC</TabsTrigger></TabsList>
          <TabsContent value="saml" className="space-y-4 mt-4"><div><Label>Entity ID</Label><Input placeholder="https://your-idp.com/entity" /></div><div><Label>SSO URL</Label><Input placeholder="https://your-idp.com/sso" /></div><div><Label>SLO URL</Label><Input placeholder="https://your-idp.com/slo" /></div><div><Label>X.509 Certificate</Label><Textarea placeholder="-----BEGIN CERTIFICATE-----&#10;...&#10;-----END CERTIFICATE-----" className="font-mono text-xs" /></div></TabsContent>
          <TabsContent value="oidc" className="space-y-4 mt-4"><div><Label>Issuer URL</Label><Input placeholder="https://your-idp.com" /></div><div><Label>Client ID</Label><Input placeholder="your-client-id" /></div><div><Label>Client Secret</Label><Input type="password" placeholder="your-client-secret" /></div><div><Label>Authorization URL</Label><Input placeholder="https://your-idp.com/authorize" /></div></TabsContent>
        </Tabs>
        <div><Label>Domain Whitelist</Label><Input placeholder="company.com, university.edu" /><p className="text-xs text-muted-foreground mt-1">Comma-separated list of allowed email domains</p></div>
        <div className="flex gap-3"><Button style={{ backgroundColor: PRIMARY }}>Save Configuration</Button><Button variant="outline">Test Connection</Button></div>
      </CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 17. AUDIT LOGS SCREEN
// ═══════════════════════════════════════════
function AuditLogsScreen() {
  const [filter, setFilter] = useState('all');
  const { data, loading, error } = useAuditLogs();
  const logs = (data?.items || []) as any[];
  const actionTypes = [...new Set(logs.map((l: any) => (l.action || '').split('.')[0]).filter(Boolean))];
  const filtered = filter === 'all' ? logs : logs.filter((l: any) => (l.action || '').startsWith(filter));
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Audit Logs" desc="Track all platform activity" />
      <div className="flex items-center gap-2 mb-2 flex-wrap"><Badge variant={filter === 'all' ? 'default' : 'outline'} className="cursor-pointer" onClick={() => setFilter('all')}>All</Badge>{actionTypes.map(t => <Badge key={t} variant={filter === t ? 'default' : 'outline'} className="cursor-pointer" onClick={() => setFilter(t)}>{t}</Badge>)}</div>
      {loading && <Card><CardContent className="p-8 text-center text-muted-foreground">Loading audit logs…</CardContent></Card>}
      {error && <Card><CardContent className="p-8 text-center text-red-600">{error}</CardContent></Card>}
      {!loading && !error && filtered.length === 0 && <Card><CardContent className="p-8 text-center text-muted-foreground">No audit log entries yet.</CardContent></Card>}
      {filtered.length > 0 && <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Timestamp</TableHead><TableHead>User</TableHead><TableHead>Action</TableHead><TableHead>Resource</TableHead><TableHead>IP Address</TableHead></TableRow></TableHeader>
        <TableBody>{filtered.map((l: any, i: number) => (<TableRow key={l.id || i}><TableCell className="font-mono text-xs">{l.createdAt ? new Date(l.createdAt).toLocaleString() : '—'}</TableCell><TableCell>{l.actorName}</TableCell><TableCell><Badge variant="outline" className="text-xs font-mono">{l.action}</Badge></TableCell><TableCell className="text-sm">{l.resource}</TableCell><TableCell className="font-mono text-xs text-muted-foreground">{l.ip || '—'}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>}
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 18. FEATURE FLAGS SCREEN
// ═══════════════════════════════════════════
function FeatureFlagsScreen() {
  const [flags, setFlags] = useState([
    { name: 'kg_v2_engine', desc: 'Knowledge Graph V2 engine', enabled: true, rollout: 100, group: 'All Users' },
    { name: 'advanced_safety', desc: 'Advanced safety profiling', enabled: true, rollout: 75, group: 'Beta' },
    { name: 'batch_export', desc: 'Batch export for reports', enabled: false, rollout: 0, group: 'Internal' },
    { name: 'realtime_collab', desc: 'Real-time collaboration', enabled: true, rollout: 25, group: 'Beta' },
    { name: 'ai_explanations', desc: 'AI-powered explanations', enabled: true, rollout: 100, group: 'All Users' },
    { name: 'dark_mode', desc: 'Dark mode theme', enabled: false, rollout: 0, group: 'Internal' },
  ]);
  const toggleFlag = (name: string) => setFlags(prev => prev.map(f => f.name === name ? { ...f, enabled: !f.enabled, rollout: !f.enabled ? 100 : 0 } : f));
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Feature Flags" desc="Control feature rollouts and experiments" actions={<Button style={{ backgroundColor: PRIMARY }}><Plus className="h-4 w-4 mr-1.5" />Create Flag</Button>} />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Flag</TableHead><TableHead>Description</TableHead><TableHead>Status</TableHead><TableHead>Rollout</TableHead><TableHead>Target Group</TableHead></TableRow></TableHeader>
        <TableBody>{flags.map(f => (<TableRow key={f.name}><TableCell className="font-mono text-sm font-medium">{f.name}</TableCell><TableCell className="text-sm text-muted-foreground">{f.desc}</TableCell>
          <TableCell><Switch checked={f.enabled} onCheckedChange={() => toggleFlag(f.name)} /></TableCell><TableCell><div className="flex items-center gap-2"><Progress value={f.rollout} className="w-16 h-2" /><span className="text-xs">{f.rollout}%</span></div></TableCell>
          <TableCell><Badge variant="outline">{f.group}</Badge></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}


// ═══════════════════════════════════════════
// 19. API DOCS SCREEN
// ═══════════════════════════════════════════
function APIDocsScreen() {
  const [activeEndpoint, setActiveEndpoint] = useState('query');
  const endpoints = [
    { id: 'query', method: 'POST', path: '/v1/query', desc: 'Execute a disease query' },
    { id: 'candidates', method: 'GET', path: '/v1/candidates/{id}', desc: 'Get candidate details' },
    { id: 'explain', method: 'POST', path: '/v1/explain', desc: 'Get AI explanation' },
    { id: 'safety', method: 'GET', path: '/v1/safety/{drugId}', desc: 'Safety profile for drug' },
    { id: 'report', method: 'POST', path: '/v1/report/generate', desc: 'Generate evidence report' },
    { id: 'kg', method: 'GET', path: '/v1/kg/explore', desc: 'Explore knowledge graph' },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="API Documentation" desc="RESTful API reference for DrugOS integration" actions={<Button variant="outline" size="sm"><BookOpen className="h-4 w-4 mr-1.5" />OpenAPI Spec</Button>} />
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        <div className="space-y-1">{endpoints.map(ep => (<button key={ep.id} onClick={() => setActiveEndpoint(ep.id)} className={`w-full text-left p-3 rounded-lg text-sm transition-colors ${activeEndpoint === ep.id ? 'bg-primary/10 text-primary font-medium' : 'hover:bg-accent'}`}>
          <div className="flex items-center gap-2"><Badge className={`text-[10px] ${ep.method === 'GET' ? 'bg-green-100 text-green-700' : 'bg-blue-100 text-blue-700'}`}>{ep.method}</Badge><span className="font-mono text-xs">{ep.path}</span></div><p className="text-xs text-muted-foreground mt-1">{ep.desc}</p>
        </button>))}</div>
        <div className="lg:col-span-3"><Card><CardHeader><CardTitle className="text-base flex items-center gap-2"><Badge className="bg-blue-100 text-blue-700">POST</Badge><code className="text-sm">/v1/query</code></CardTitle><CardDescription>Execute a disease query and return ranked candidates</CardDescription></CardHeader>
          <CardContent className="space-y-4"><div><h4 className="text-sm font-semibold mb-2">Request Body</h4><pre className="bg-slate-950 text-green-400 p-4 rounded-lg text-xs overflow-x-auto">{`{
  "disease": "Huntington's Disease",
  "filters": {
    "safety_tier": ["green", "yellow"],
    "min_score": 60,
    "therapeutic_area": "Neurology"
  },
  "limit": 20
}`}</pre></div>
            <div><h4 className="text-sm font-semibold mb-2">Response (200 OK)</h4><pre className="bg-slate-950 text-green-400 p-4 rounded-lg text-xs overflow-x-auto">{`{
  "query_id": "q_abc123",
  "disease": "Huntington's Disease",
  "candidates": [
    { "drug": "Memantine", "score": 87, "safety": "green" }
  ],
  "total": 12
}`}</pre></div>
            <div><h4 className="text-sm font-semibold mb-2">Authentication</h4><p className="text-sm text-muted-foreground">All API requests require a Bearer token in the Authorization header: <code className="bg-muted px-1.5 py-0.5 rounded text-xs">Authorization: Bearer your-api-key</code></p></div>
          </CardContent></Card></div>
      </div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 20. API KEYS SCREEN
// ═══════════════════════════════════════════
function APIKeysScreen() {
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [createErr, setCreateErr] = useState<string | null>(null);
  const [revokeId, setRevokeId] = useState<string | null>(null);
  const { data, loading, error, refetch } = useApiKeys();
  const keys = (data?.items || []) as any[];

  const handleCreate = async () => {
    if (!newName.trim()) { setCreateErr('Name is required'); return; }
    setCreating(true); setCreateErr(null);
    try {
      const result = await api.apiKeys.create({ name: newName });
      setNewKey(result.rawKey);
      setNewName('');
      await refetch();
    } catch (e) {
      setCreateErr(errorMessage(e));
    } finally { setCreating(false); }
  };

  const handleRevoke = async (id: string) => {
    if (!confirm('Revoke this API key? This cannot be undone.')) return;
    setRevokeId(id);
    try {
      await api.apiKeys.revoke(id);
      await refetch();
    } catch (e) {
      alert(errorMessage(e));
    } finally { setRevokeId(null); }
  };

  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="API Keys" desc="Manage your API authentication keys" actions={<Button style={{ backgroundColor: PRIMARY }} onClick={() => { setCreateOpen(true); setNewKey(null); }}><Plus className="h-4 w-4 mr-1.5" />Create Key</Button>} />
      {loading && <Card><CardContent className="p-8 text-center text-muted-foreground">Loading API keys…</CardContent></Card>}
      {error && <Card><CardContent className="p-8 text-center text-red-600">{error}</CardContent></Card>}
      {!loading && !error && keys.length === 0 && <Card><CardContent className="p-8 text-center text-muted-foreground">No API keys yet — create one to start using the DrugOS API.</CardContent></Card>}
      {keys.length > 0 && <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Name</TableHead><TableHead>Key Prefix</TableHead><TableHead>Created</TableHead><TableHead>Last Used</TableHead><TableHead>Status</TableHead><TableHead></TableHead></TableRow></TableHeader>
        <TableBody>{keys.map(k => (<TableRow key={k.id}><TableCell className="font-medium">{k.name}</TableCell><TableCell className="font-mono text-xs">{k.prefix}…</TableCell><TableCell className="text-sm">{k.createdAt ? new Date(k.createdAt).toLocaleDateString() : '—'}</TableCell><TableCell className="text-sm text-muted-foreground">{k.lastUsedAt ? new Date(k.lastUsedAt).toLocaleDateString() : 'Never'}</TableCell>
          <TableCell><Badge variant={k.revokedAt ? 'destructive' : 'default'}>{k.revokedAt ? 'revoked' : 'active'}</Badge></TableCell><TableCell><Button variant="ghost" size="sm" className="h-7 text-red-500" disabled={revokeId === k.id || !!k.revokedAt} onClick={() => handleRevoke(k.id)}>{revokeId === k.id ? 'Revoking…' : 'Revoke'}</Button></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogContent><DialogHeader><DialogTitle>{newKey ? 'API Key Created' : 'Create New API Key'}</DialogTitle><DialogDescription>{newKey ? 'Copy this key now — it will not be shown again.' : 'Generate a new API key for programmatic access'}</DialogDescription></DialogHeader>
        {newKey ? (
          <div className="space-y-3"><div className="bg-slate-950 text-green-400 p-4 rounded-lg font-mono text-xs break-all">{newKey}</div><Button variant="outline" className="w-full" onClick={() => { navigator.clipboard.writeText(newKey); }}><Copy className="h-4 w-4 mr-1.5" />Copy to clipboard</Button></div>
        ) : (
          <div className="space-y-4"><div><Label>Key Name</Label><Input placeholder="e.g. Production Integration" value={newName} onChange={e => setNewName(e.target.value)} /></div>
          {createErr && <p className="text-sm text-red-600">{createErr}</p>}
          </div>
        )}
        <DialogFooter><Button variant="outline" onClick={() => setCreateOpen(false)}>{newKey ? 'Done' : 'Cancel'}</Button>{!newKey && <Button style={{ backgroundColor: PRIMARY }} onClick={handleCreate} disabled={creating}>{creating ? 'Creating…' : 'Create Key'}</Button>}</DialogFooter>
      </DialogContent></Dialog>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 21. PLAYGROUND SCREEN
// ═══════════════════════════════════════════
function PlaygroundScreen() {
  const [endpoint, setEndpoint] = useState('/v1/query');
  const [requestBody, setRequestBody] = useState('{\n  "disease": "Huntington\'s Disease",\n  "limit": 5\n}');
  const [response, setResponse] = useState('');
  const [loading, setLoading] = useState(false);
  const executeQuery = () => { setLoading(true); setTimeout(() => { setResponse(JSON.stringify({ query_id: "q_mock_123", disease: "Huntington's Disease", candidates: [{ drug: "Memantine", score: 87, safety: "green" }, { drug: "Sirolimus", score: 82, safety: "green" }, { drug: "Riluzole", score: 76, safety: "yellow" }], total: 3, execution_time: "1.23s" }, null, 2)); setLoading(false); }, 1500); };
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="API Playground" desc="Test DrugOS API endpoints interactively" />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Request</CardTitle></CardHeader><CardContent className="space-y-4">
          <div><Label>Endpoint</Label><Select value={endpoint} onValueChange={setEndpoint}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="/v1/query">POST /v1/query</SelectItem><SelectItem value="/v1/candidates">GET /v1/candidates</SelectItem><SelectItem value="/v1/explain">POST /v1/explain</SelectItem><SelectItem value="/v1/safety">GET /v1/safety</SelectItem></SelectContent></Select></div>
          <div><Label>Headers</Label><div className="bg-muted p-3 rounded-lg text-xs font-mono"><div>Authorization: Bearer dros_prod_****7a3f</div><div>Content-Type: application/json</div></div></div>
          <div><Label>Body</Label><Textarea value={requestBody} onChange={e => setRequestBody(e.target.value)} className="font-mono text-xs min-h-[200px]" /></div>
          <Button className="w-full" style={{ backgroundColor: PRIMARY }} onClick={executeQuery} disabled={loading}>{loading ? <><RefreshCw className="h-4 w-4 mr-1.5 animate-spin" />Executing...</> : <><Play className="h-4 w-4 mr-1.5" />Execute</>}</Button>
        </CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Response</CardTitle></CardHeader><CardContent>{response ? <pre className="bg-slate-950 text-green-400 p-4 rounded-lg text-xs overflow-x-auto min-h-[300px]">{response}</pre> : <div className="flex items-center justify-center h-[300px] text-muted-foreground"><div className="text-center"><Code className="h-8 w-8 mx-auto mb-2 opacity-30" /><p>Execute a request to see the response</p></div></div>}</CardContent></Card>
      </div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 22. WEBHOOKS SCREEN
// ═══════════════════════════════════════════
function WebhooksScreen() {
  const [createOpen, setCreateOpen] = useState(false);
  const webhooksList = [
    { url: 'https://api.pharma.com/webhooks/drugos', events: ['candidate.found', 'report.ready'], status: 'active', lastDelivery: '2 hours ago', successRate: 99.2 },
    { url: 'https://staging.pharma.com/hooks/drugos', events: ['query.completed'], status: 'active', lastDelivery: '1 day ago', successRate: 95.0 },
    { url: 'https://old-api.partner.com/wh', events: ['candidate.found'], status: 'failing', lastDelivery: '3 days ago', successRate: 42.0 },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Webhooks" desc="Configure webhook endpoints for event notifications" actions={<Button style={{ backgroundColor: PRIMARY }} onClick={() => setCreateOpen(true)}><Plus className="h-4 w-4 mr-1.5" />Add Webhook</Button>} />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>URL</TableHead><TableHead>Events</TableHead><TableHead>Status</TableHead><TableHead>Last Delivery</TableHead><TableHead>Success Rate</TableHead></TableRow></TableHeader>
        <TableBody>{webhooksList.map(w => (<TableRow key={w.url}><TableCell className="font-mono text-xs max-w-[200px] truncate">{w.url}</TableCell><TableCell><div className="flex flex-wrap gap-1">{w.events.map(e => <Badge key={e} variant="outline" className="text-[10px]">{e}</Badge>)}</div></TableCell>
          <TableCell><Badge variant={w.status === 'active' ? 'default' : 'destructive'}>{w.status}</Badge></TableCell><TableCell className="text-sm text-muted-foreground">{w.lastDelivery}</TableCell><TableCell><span className={w.successRate > 90 ? 'text-green-600' : 'text-red-500'}>{w.successRate}%</span></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
      <Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogContent><DialogHeader><DialogTitle>Add Webhook</DialogTitle></DialogHeader><div className="space-y-4"><div><Label>Endpoint URL</Label><Input placeholder="https://your-api.com/webhooks/drugos" /></div><div><Label>Events</Label><div className="space-y-2 mt-2"><div className="flex items-center gap-2"><Checkbox id="ev-candidate" defaultChecked /><label htmlFor="ev-candidate" className="text-sm">candidate.found</label></div><div className="flex items-center gap-2"><Checkbox id="ev-report" defaultChecked /><label htmlFor="ev-report" className="text-sm">report.ready</label></div><div className="flex items-center gap-2"><Checkbox id="ev-query" /><label htmlFor="ev-query" className="text-sm">query.completed</label></div></div></div></div><DialogFooter><Button style={{ backgroundColor: PRIMARY }} onClick={() => setCreateOpen(false)}>Create Webhook</Button></DialogFooter></DialogContent></Dialog>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 23. PROFILE SCREEN
// ═══════════════════════════════════════════
function ProfileScreen() {
  const [name, setName] = useState('Dr. Sarah Chen');
  const [email, setEmail] = useState('sarah@pharma.com');
  const [title, setTitle] = useState('Principal Scientist');
  const [org, setOrg] = useState('PharmaCorp');
  const [bio, setBio] = useState('Leading drug repurposing research for rare neurological diseases.');
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Profile" desc="Manage your personal information" />
      <Card><CardContent className="p-6"><div className="flex items-start gap-6"><Avatar className="h-20 w-20"><AvatarFallback className="bg-primary text-white text-2xl">SC</AvatarFallback></Avatar><div><Button variant="outline" size="sm">Change Avatar</Button><p className="text-xs text-muted-foreground mt-1">JPG, PNG. Max 2MB.</p></div></div></CardContent></Card>
      <Card><CardContent className="p-6 space-y-4"><div className="grid grid-cols-1 sm:grid-cols-2 gap-4"><div><Label>Full Name</Label><Input value={name} onChange={e => setName(e.target.value)} /></div><div><Label>Email</Label><Input value={email} onChange={e => setEmail(e.target.value)} /></div><div><Label>Title</Label><Input value={title} onChange={e => setTitle(e.target.value)} /></div><div><Label>Organization</Label><Input value={org} onChange={e => setOrg(e.target.value)} /></div></div><div><Label>Bio</Label><Textarea value={bio} onChange={e => setBio(e.target.value)} className="min-h-[80px]" /></div><div className="flex justify-end"><Button style={{ backgroundColor: PRIMARY }}>Save Changes</Button></div></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 24. SECURITY SETTINGS SCREEN
// ═══════════════════════════════════════════
function SecuritySettingsScreen() {
  const [twoFA, setTwoFA] = useState(true);
  const sessions = [
    { device: 'Chrome on macOS', location: 'San Francisco, US', current: true, lastActive: 'Now' },
    { device: 'Safari on iPhone', location: 'San Francisco, US', current: false, lastActive: '2 hours ago' },
    { device: 'Firefox on Windows', location: 'New York, US', current: false, lastActive: '1 week ago' },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Security Settings" desc="Manage your account security" />
      <Card><CardContent className="p-6 space-y-6"><div><h3 className="font-semibold mb-2">Change Password</h3><div className="space-y-3 max-w-md"><div><Label>Current Password</Label><Input type="password" /></div><div><Label>New Password</Label><Input type="password" /></div><div><Label>Confirm New Password</Label><Input type="password" /></div><Button style={{ backgroundColor: PRIMARY }}>Update Password</Button></div></div>
        <Separator /><div className="flex items-center justify-between"><div><h3 className="font-semibold">Two-Factor Authentication</h3><p className="text-sm text-muted-foreground">Add an extra layer of security to your account</p></div><Switch checked={twoFA} onCheckedChange={setTwoFA} /></div>
        <Separator /><div><h3 className="font-semibold mb-3">Active Sessions</h3><div className="space-y-3">{sessions.map(s => (<div key={s.device} className="flex items-center justify-between p-3 border rounded-lg"><div className="flex items-center gap-3"><Monitor className="h-5 w-5 text-muted-foreground" /><div><p className="text-sm font-medium">{s.device}</p><p className="text-xs text-muted-foreground">{s.location} · {s.lastActive}</p></div></div>{s.current ? <Badge>Current</Badge> : <Button variant="outline" size="sm">Revoke</Button>}</div>))}</div></div>
      </CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 25. NOTIFICATIONS SCREEN
// ═══════════════════════════════════════════
function NotificationsScreen() {
  const [prefs, setPrefs] = useState({ emailQuery: true, emailReport: true, emailCollab: false, inlineQuery: true, inlineReport: true, inlineCollab: true, pushQuery: false, pushReport: true, pushCollab: false });
  const toggle = (key: keyof typeof prefs) => setPrefs(prev => ({ ...prev, [key]: !prev[key] }));
  const categories = [{ name: 'Query Results', emailKey: 'emailQuery' as const, inlineKey: 'inlineQuery' as const, pushKey: 'pushQuery' as const },{ name: 'Report Ready', emailKey: 'emailReport' as const, inlineKey: 'inlineReport' as const, pushKey: 'pushReport' as const },{ name: 'Collaboration', emailKey: 'emailCollab' as const, inlineKey: 'inlineCollab' as const, pushKey: 'pushCollab' as const }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Notification Preferences" desc="Choose how you want to be notified" />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Category</TableHead><TableHead className="text-center">Email</TableHead><TableHead className="text-center">In-App</TableHead><TableHead className="text-center">Push</TableHead></TableRow></TableHeader>
        <TableBody>{categories.map(c => (<TableRow key={c.name}><TableCell className="font-medium">{c.name}</TableCell><TableCell className="text-center"><Switch checked={prefs[c.emailKey]} onCheckedChange={() => toggle(c.emailKey)} /></TableCell><TableCell className="text-center"><Switch checked={prefs[c.inlineKey]} onCheckedChange={() => toggle(c.inlineKey)} /></TableCell><TableCell className="text-center"><Switch checked={prefs[c.pushKey]} onCheckedChange={() => toggle(c.pushKey)} /></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
      <Card><CardContent className="p-6 space-y-4"><div><Label>Digest Frequency</Label><Select defaultValue="daily"><SelectTrigger className="w-48"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="realtime">Real-time</SelectItem><SelectItem value="hourly">Hourly</SelectItem><SelectItem value="daily">Daily</SelectItem><SelectItem value="weekly">Weekly</SelectItem></SelectContent></Select></div><div><Label>Quiet Hours</Label><div className="flex items-center gap-2"><Input type="time" defaultValue="22:00" className="w-28" /><span className="text-sm">to</span><Input type="time" defaultValue="08:00" className="w-28" /></div></div><Button style={{ backgroundColor: PRIMARY }}>Save Preferences</Button></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 26. PREFERENCES SCREEN
// ═══════════════════════════════════════════
function PreferencesScreen() {
  const [theme, setTheme] = useState('light');
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Preferences" desc="Customize your DrugOS experience" />
      <Card><CardContent className="p-6 space-y-6">
        <div><Label>Theme</Label><div className="flex gap-3 mt-2">{['light', 'dark', 'system'].map(t => (<button key={t} onClick={() => setTheme(t)} className={`flex items-center gap-2 px-4 py-2 rounded-lg border text-sm transition-colors ${theme === t ? 'border-primary bg-primary/5 text-primary' : 'hover:bg-accent'}`}>{t === 'light' ? <Monitor className="h-4 w-4" /> : t === 'dark' ? <Monitor className="h-4 w-4" /> : <Smartphone className="h-4 w-4" />}{t.charAt(0).toUpperCase() + t.slice(1)}</button>))}</div></div>
        <Separator /><div><Label>Default Therapeutic Area</Label><Select defaultValue="all"><SelectTrigger className="w-64"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All Areas</SelectItem><SelectItem value="Neurology">Neurology</SelectItem><SelectItem value="Oncology">Oncology</SelectItem><SelectItem value="Rare Disease">Rare Disease</SelectItem></SelectContent></Select></div>
        <div><Label>Results Per Page</Label><Select defaultValue="20"><SelectTrigger className="w-32"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="10">10</SelectItem><SelectItem value="20">20</SelectItem><SelectItem value="50">50</SelectItem></SelectContent></Select></div>
        <Separator /><div className="flex items-center justify-between"><div><Label>Auto-save Queries</Label><p className="text-xs text-muted-foreground">Automatically save search queries to history</p></div><Switch defaultChecked /></div>
        <div className="flex items-center justify-between"><div><Label>Default Export Format</Label></div><Select defaultValue="csv"><SelectTrigger className="w-32"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="csv">CSV</SelectItem><SelectItem value="json">JSON</SelectItem><SelectItem value="xlsx">Excel</SelectItem></SelectContent></Select></div>
        <Button style={{ backgroundColor: PRIMARY }}>Save Preferences</Button>
      </CardContent></Card>
    </div></FadeIn>
  );
}


// ═══════════════════════════════════════════
// 27. PRIVACY POLICY SCREEN
// ═══════════════════════════════════════════
function PrivacyPolicyScreen() {
  const sections = [
    { title: '1. Information We Collect', content: 'DrugOS collects information that you provide directly to us, including your name, email address, organization affiliation, and research interests. We also automatically collect certain information when you use our platform, such as your IP address, browser type, operating system, referring URLs, and information about how you interact with the platform. This includes query logs, page views, and feature usage data that helps us improve our services and provide better drug repurposing insights.' },
    { title: '2. How We Use Your Information', content: 'We use the information we collect to provide, maintain, and improve the DrugOS platform, process your queries and drug repurposing analyses, send you technical notices and support messages, respond to your comments and questions, monitor and analyze trends and usage, detect and prevent fraud, and facilitate contests and promotional activities. Your research data is processed solely to deliver drug repurposing results and is never shared with third parties without explicit consent.' },
    { title: '3. Information Sharing', content: 'DrugOS does not sell, trade, or rent your personal information to third parties. We may share information with your organization administrators as part of team collaboration features, with service providers who assist in operating the platform, and when required by law. All data sharing complies with HIPAA, GDPR, and other applicable regulations.' },
    { title: '4. Data Security', content: 'We implement industry-standard security measures including encryption at rest (AES-256) and in transit (TLS 1.3), role-based access controls, regular security audits, and SOC 2 Type II compliance. We maintain Business Associate Agreements (BAA) for healthcare data processing. Our infrastructure is hosted on HIPAA-compliant cloud providers with multi-region redundancy.' },
    { title: '5. Your Rights', content: 'Under GDPR, you have the right to access, rectify, erase, and port your personal data. You may also object to or restrict certain processing activities. Under CCPA, you have the right to know, delete, and opt-out of the sale of your personal information. We provide self-service tools and support to exercise these rights.' },
    { title: '6. Data Retention', content: 'We retain your personal data for as long as your account is active or as needed to provide services. Query results and research data are retained according to your organization retention policy. You may request deletion of your data at any time through account settings or by contacting our privacy team.' },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Privacy Policy" desc="Last updated: June 1, 2026" />
      <Card><CardContent className="p-6 max-w-3xl mx-auto"><h2 className="text-xl font-bold mb-6">DrugOS Privacy Policy</h2><div className="space-y-6">{sections.map(s => (<div key={s.title}><h3 className="font-semibold text-lg mb-2">{s.title}</h3><p className="text-sm text-muted-foreground leading-relaxed">{s.content}</p></div>))}</div></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 28. TERMS SCREEN
// ═══════════════════════════════════════════
function TermsScreen() {
  const sections = [
    { title: '1. Acceptance of Terms', content: 'By accessing or using the DrugOS platform, you agree to be bound by these Terms of Service. If you do not agree to these terms, you may not access or use the platform. These terms apply to all visitors, users, and others who access or use DrugOS. We reserve the right to modify these terms at any time, and your continued use after modification constitutes acceptance of the updated terms.' },
    { title: '2. Use License', content: 'Subject to your compliance with these Terms, DrugOS grants you a limited, non-exclusive, non-transferable, revocable license to access and use the platform for your internal business or academic research purposes. You may not sublicense, sell, or distribute access to the platform. Usage limits apply based on your subscription plan, and exceeding these limits may result in overage charges or service restrictions.' },
    { title: '3. Intellectual Property', content: 'The DrugOS platform, including its knowledge graph, scoring algorithms, and AI models, is the exclusive property of DrugOS Corp. Drug repurposing predictions and evidence packages generated by the platform are provided for research purposes. Discovery Deal licensees receive exclusive commercial rights to validated predictions as defined in their licensing agreements.' },
    { title: '4. Prohibited Uses', content: 'You may not use DrugOS to develop competing products or services, reverse engineer the platform or its algorithms, share your account credentials with unauthorized users, or use the platform for any unlawful purpose. Violation of these restrictions may result in immediate account termination and legal action.' },
    { title: '5. Limitation of Liability', content: 'DrugOS provides computational predictions for research purposes only. We do not guarantee the accuracy, completeness, or clinical validity of any prediction. Users are responsible for independent validation before clinical application. DrugOS shall not be liable for any indirect, incidental, or consequential damages arising from the use of the platform.' },
    { title: '6. Termination', content: 'We may terminate or suspend your account immediately, without prior notice, for any breach of these Terms. Upon termination, your right to use the platform will cease immediately. Provisions that by their nature should survive termination shall remain in effect.' },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Terms of Service" desc="Last updated: June 1, 2026 · Version 3.2" />
      <Card><CardContent className="p-6 max-w-3xl mx-auto"><h2 className="text-xl font-bold mb-6">DrugOS Terms of Service</h2><div className="space-y-6">{sections.map(s => (<div key={s.title}><h3 className="font-semibold text-lg mb-2">{s.title}</h3><p className="text-sm text-muted-foreground leading-relaxed">{s.content}</p></div>))}</div></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 29. COMPLIANCE SCREEN
// ═══════════════════════════════════════════
function ComplianceScreen() {
  const complianceItems = [
    { name: 'HIPAA', status: 'compliant', details: 'Business Associate Agreement available', lastAudit: 'Mar 2026', icon: ShieldCheck },
    { name: 'GDPR', status: 'compliant', details: 'EU data processing agreement in place', lastAudit: 'Apr 2026', icon: Globe },
    { name: 'SOC 2 Type II', status: 'compliant', details: 'Annual audit completed by Big 4 firm', lastAudit: 'Feb 2026', icon: CheckCircle2 },
    { name: '21 CFR Part 11', status: 'partial', details: 'Electronic signatures in beta', lastAudit: 'Pending', icon: FileText },
    { name: 'GxP Validated', status: 'compliant', details: 'GxP validated mode for clinical research', lastAudit: 'May 2026', icon: Award },
    { name: 'ISO 27001', status: 'in_progress', details: 'Certification expected Q3 2026', lastAudit: 'In progress', icon: Lock },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Compliance" desc="Regulatory compliance and certifications" />
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard title="Compliant" value={complianceItems.filter(c => c.status === 'compliant').length} icon={CheckCircle2} /><StatCard title="In Progress" value={complianceItems.filter(c => c.status === 'in_progress' || c.status === 'partial').length} icon={Clock} /><StatCard title="Certifications" value={6} icon={Award} />
      </div>
      <div className="space-y-4">{complianceItems.map(item => { const Icon = item.icon; return (<Card key={item.name} className="hover:shadow-md transition-shadow"><CardContent className="p-5"><div className="flex items-start justify-between"><div className="flex items-start gap-4"><div className="p-3 rounded-lg bg-primary/10"><Icon className="h-6 w-6 text-primary" /></div><div><h3 className="font-semibold">{item.name}</h3><p className="text-sm text-muted-foreground mt-0.5">{item.details}</p><p className="text-xs text-muted-foreground mt-1">Last audit: {item.lastAudit}</p></div></div><Badge variant={item.status === 'compliant' ? 'default' : item.status === 'partial' ? 'secondary' : 'outline'}>{item.status}</Badge></div></CardContent></Card>); })}</div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 30. HELP CENTER SCREEN
// ═══════════════════════════════════════════
function HelpCenterScreen() {
  const [search, setSearch] = useState('');
  const categories = [{ title: 'Getting Started', articles: 8, icon: Play },{ title: 'Search & Queries', articles: 12, icon: Search },{ title: 'Drug Candidates', articles: 10, icon: Target },{ title: 'Evidence & Reports', articles: 7, icon: FileText },{ title: 'API & Integration', articles: 15, icon: Code },{ title: 'Billing & Plans', articles: 6, icon: CreditCard }];
  const popular = [{ title: 'How to search for diseases', views: '2.4K' },{ title: 'Understanding composite scores', views: '1.8K' },{ title: 'Exporting candidate reports', views: '1.5K' },{ title: 'Setting up API access', views: '1.2K' },{ title: 'Managing team permissions', views: '980' }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Help Center" desc="Find answers and get support" />
      <Card className="bg-gradient-to-r from-primary/5 to-primary/10"><CardContent className="p-8 text-center"><h2 className="text-xl font-bold mb-3">How can we help?</h2><div className="relative max-w-lg mx-auto"><Search className="absolute left-4 top-1/2 -translate-y-1/2 h-5 w-5 text-muted-foreground" /><Input placeholder="Search help articles..." value={search} onChange={e => setSearch(e.target.value)} className="pl-12 h-12 text-base" /></div></CardContent></Card>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">{categories.map(c => { const Icon = c.icon; return (<Card key={c.title} className="hover:shadow-md transition-shadow cursor-pointer"><CardContent className="p-5"><div className="flex items-center gap-3 mb-2"><Icon className="h-5 w-5 text-primary" /><h3 className="font-semibold text-sm">{c.title}</h3></div><p className="text-xs text-muted-foreground">{c.articles} articles</p></CardContent></Card>); })}</div>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Popular Articles</CardTitle></CardHeader><CardContent><div className="space-y-2">{popular.map(a => (<button key={a.title} className="w-full flex items-center justify-between p-3 rounded-lg hover:bg-accent text-left transition-colors"><span className="text-sm font-medium">{a.title}</span><span className="text-xs text-muted-foreground">{a.views} views</span></button>))}</div></CardContent></Card>
      <div className="text-center"><Button variant="outline" onClick={() => {}}><MessageSquare className="h-4 w-4 mr-2" />Contact Support</Button></div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 31. TICKET SCREEN
// ═══════════════════════════════════════════
function TicketScreen() {
  const [createOpen, setCreateOpen] = useState(false);
  const tickets = [
    { id: 'TK-1234', subject: 'Cannot export report in PDF format', status: 'open', priority: 'high', created: '2 hours ago', messages: 3 },
    { id: 'TK-1233', subject: 'API rate limit hit unexpectedly', status: 'in-progress', priority: 'medium', created: '1 day ago', messages: 5 },
    { id: 'TK-1230', subject: 'Knowledge graph timeout for rare disease', status: 'open', priority: 'low', created: '2 days ago', messages: 2 },
    { id: 'TK-1228', subject: 'Feature request: batch comparison', status: 'closed', priority: 'low', created: '1 week ago', messages: 4 },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Support Tickets" desc={`${tickets.filter(t => t.status !== 'closed').length} open tickets`} actions={<Button style={{ backgroundColor: PRIMARY }} onClick={() => setCreateOpen(true)}><Plus className="h-4 w-4 mr-1.5" />New Ticket</Button>} />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Ticket</TableHead><TableHead>Subject</TableHead><TableHead>Status</TableHead><TableHead>Priority</TableHead><TableHead>Created</TableHead><TableHead>Messages</TableHead></TableRow></TableHeader>
        <TableBody>{tickets.map(t => (<TableRow key={t.id} className="cursor-pointer hover:bg-muted/30"><TableCell className="font-mono text-sm">{t.id}</TableCell><TableCell className="font-medium">{t.subject}</TableCell>
          <TableCell><Badge variant={t.status === 'open' ? 'default' : t.status === 'in-progress' ? 'secondary' : 'outline'}>{t.status}</Badge></TableCell>
          <TableCell><Badge variant={t.priority === 'high' ? 'destructive' : t.priority === 'medium' ? 'secondary' : 'outline'}>{t.priority}</Badge></TableCell>
          <TableCell className="text-sm text-muted-foreground">{t.created}</TableCell><TableCell>{t.messages}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
      <Dialog open={createOpen} onOpenChange={setCreateOpen}><DialogContent><DialogHeader><DialogTitle>Create Support Ticket</DialogTitle></DialogHeader><div className="space-y-4"><div><Label>Subject</Label><Input placeholder="Brief description of the issue" /></div><div><Label>Priority</Label><Select defaultValue="medium"><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="low">Low</SelectItem><SelectItem value="medium">Medium</SelectItem><SelectItem value="high">High</SelectItem><SelectItem value="critical">Critical</SelectItem></SelectContent></Select></div><div><Label>Description</Label><Textarea placeholder="Provide details about the issue..." className="min-h-[100px]" /></div></div><DialogFooter><Button style={{ backgroundColor: PRIMARY }} onClick={() => setCreateOpen(false)}>Submit Ticket</Button></DialogFooter></DialogContent></Dialog>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 32. SYSTEM STATUS SCREEN
// ═══════════════════════════════════════════
function SystemStatusScreen() {
  const services = [
    { name: 'API Gateway', status: 'operational', uptime: '99.98%', responseTime: '45ms' },
    { name: 'Knowledge Graph', status: 'operational', uptime: '99.95%', responseTime: '120ms' },
    { name: 'Search Engine', status: 'operational', uptime: '99.97%', responseTime: '89ms' },
    { name: 'Database', status: 'operational', uptime: '99.99%', responseTime: '12ms' },
    { name: 'Report Generator', status: 'degraded', uptime: '99.50%', responseTime: '3.2s' },
    { name: 'Authentication', status: 'operational', uptime: '99.99%', responseTime: '23ms' },
  ];
  const incidents = [{ date: 'Jun 10, 2026', title: 'Report generation delays', status: 'Monitoring', duration: '2h 15m' },{ date: 'Jun 5, 2026', title: 'Scheduled maintenance completed', status: 'Resolved', duration: '45m' },{ date: 'May 28, 2026', title: 'API rate limiting issue', status: 'Resolved', duration: '1h 30m' }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="System Status" desc="Real-time platform health monitoring" />
      <Card className="bg-green-50 border-green-200"><CardContent className="p-5"><div className="flex items-center gap-3"><CheckCircle2 className="h-6 w-6 text-green-600" /><div><h3 className="font-semibold text-green-800">All Systems Operational</h3><p className="text-sm text-green-700">Last checked: just now</p></div></div></CardContent></Card>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Service Status</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Service</TableHead><TableHead>Status</TableHead><TableHead>Uptime (30d)</TableHead><TableHead>Response Time</TableHead></TableRow></TableHeader>
        <TableBody>{services.map(s => (<TableRow key={s.name}><TableCell className="font-medium">{s.name}</TableCell><TableCell><div className="flex items-center gap-2"><span className={`w-2.5 h-2.5 rounded-full ${s.status === 'operational' ? 'bg-green-500' : 'bg-amber-500'}`} /><Badge variant={s.status === 'operational' ? 'default' : 'secondary'}>{s.status}</Badge></div></TableCell><TableCell>{s.uptime}</TableCell><TableCell>{s.responseTime}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Recent Incidents</CardTitle></CardHeader><CardContent><div className="space-y-3">{incidents.map(inc => (<div key={inc.title} className="flex items-center justify-between p-3 border rounded-lg"><div><p className="text-sm font-medium">{inc.title}</p><p className="text-xs text-muted-foreground">{inc.date} · Duration: {inc.duration}</p></div><Badge variant={inc.status === 'Resolved' ? 'outline' : 'secondary'}>{inc.status}</Badge></div>))}</div></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 33. INVESTOR DASHBOARD SCREEN
// ═══════════════════════════════════════════
function InvestorDashboardScreen() {
  const revenueData = [{ month: 'Jan', arr: 420000, mrr: 35000 },{ month: 'Feb', arr: 480000, mrr: 40000 },{ month: 'Mar', arr: 550000, mrr: 46000 },{ month: 'Apr', arr: 620000, mrr: 52000 },{ month: 'May', arr: 720000, mrr: 60000 },{ month: 'Jun', arr: 840000, mrr: 70000 }];
  const metrics = [{ label: 'ARR', value: '$840K', trend: '+100%' },{ label: 'MRR', value: '$70K', trend: '+17%' },{ label: 'Customers', value: '42', trend: '+24%' },{ label: 'NRR', value: '118%', trend: '+8%' }];
  const cohorts = [{ cohort: 'Q1 2026', customers: 12, mrr: '$8.4K', retention: '92%' },{ cohort: 'Q4 2025', customers: 18, mrr: '$14.2K', retention: '88%' },{ cohort: 'Q3 2025', customers: 8, mrr: '$7.6K', retention: '85%' }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Investor Dashboard" desc="Key business metrics and financial overview" />
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">{metrics.map(m => (<StatCard key={m.label} title={m.label} value={m.value} trend={m.trend} icon={m.label === 'ARR' ? DollarSign : m.label === 'Customers' ? Users : m.label === 'NRR' ? TrendingUp : BarChart3} />))}</div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">ARR Growth</CardTitle></CardHeader><CardContent><div className="h-64"><ResponsiveContainer width="100%" height="100%"><AreaChart data={revenueData}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="month" /><YAxis tickFormatter={v => `$${(v/1000).toFixed(0)}K`} /><RechartsTooltip formatter={(v: number) => `$${(v/1000).toFixed(0)}K`} /><Area type="monotone" dataKey="arr" stroke={PRIMARY} fill={`${PRIMARY}20`} /></AreaChart></ResponsiveContainer></div></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Cohort Analysis</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Cohort</TableHead><TableHead>Customers</TableHead><TableHead>MRR</TableHead><TableHead>Retention</TableHead></TableRow></TableHeader>
          <TableBody>{cohorts.map(c => (<TableRow key={c.cohort}><TableCell className="font-medium">{c.cohort}</TableCell><TableCell>{c.customers}</TableCell><TableCell>{c.mrr}</TableCell><TableCell><span className="text-green-600 font-medium">{c.retention}</span></TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
      </div>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Financial Projections</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Year</TableHead><TableHead>Revenue</TableHead><TableHead>Expense</TableHead><TableHead>EBITDA</TableHead></TableRow></TableHeader>
        <TableBody>{[{ year: '2026', rev: '$0.84M', exp: '$2.1M', ebitda: '-$1.26M' },{ year: '2027', rev: '$3.5M', exp: '$3.8M', ebitda: '-$0.3M' },{ year: '2028', rev: '$8.5M', exp: '$5.2M', ebitda: '$3.3M' }].map(r => (<TableRow key={r.year}><TableCell className="font-medium">{r.year}</TableCell><TableCell>{r.rev}</TableCell><TableCell>{r.exp}</TableCell><TableCell className={r.ebitda.startsWith('-') ? 'text-red-500' : 'text-green-600'}>{r.ebitda}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 34. CAP TABLE SCREEN
// ═══════════════════════════════════════════
function CapTableScreen() {
  const shareholders = [
    { name: 'Founders', shares: '4,000,000', pct: '40%', class: 'Common', role: 'Manoj, Rohan, Aseem' },
    { name: 'Series A Investors', shares: '2,500,000', pct: '25%', class: 'Preferred', role: 'VC Fund Alpha' },
    { name: 'Angel Investors', shares: '1,000,000', pct: '10%', class: 'Preferred', role: 'Various angels' },
    { name: 'Option Pool', shares: '1,500,000', pct: '15%', class: 'Common', role: 'Employee options' },
    { name: 'SAFE Holders', shares: '1,000,000', pct: '10%', class: 'SAFE', role: 'Pre-seed investors' },
  ];
  const rounds = [{ round: 'Pre-Seed', date: 'Q3 2024', amount: '$500K', valuation: '$3M' },{ round: 'Seed', date: 'Q1 2025', amount: '$2M', valuation: '$10M' },{ round: 'Series A', date: 'Q1 2026', amount: '$8M', valuation: '$40M' }];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Cap Table" desc="Capitalization table and funding history" />
      <Card><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Shareholder</TableHead><TableHead>Shares</TableHead><TableHead>Ownership</TableHead><TableHead>Class</TableHead><TableHead>Details</TableHead></TableRow></TableHeader>
        <TableBody>{shareholders.map(s => (<TableRow key={s.name}><TableCell className="font-medium">{s.name}</TableCell><TableCell className="font-mono text-sm">{s.shares}</TableCell><TableCell><div className="flex items-center gap-2"><Progress value={parseFloat(s.pct)} className="w-16 h-2" /><span className="text-sm font-semibold">{s.pct}</span></div></TableCell><TableCell><Badge variant="outline">{s.class}</Badge></TableCell><TableCell className="text-sm text-muted-foreground">{s.role}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Funding Rounds</CardTitle></CardHeader><CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Round</TableHead><TableHead>Date</TableHead><TableHead>Amount</TableHead><TableHead>Post-Money Valuation</TableHead></TableRow></TableHeader>
        <TableBody>{rounds.map(r => (<TableRow key={r.round}><TableCell className="font-medium">{r.round}</TableCell><TableCell>{r.date}</TableCell><TableCell className="font-semibold">{r.amount}</TableCell><TableCell>{r.valuation}</TableCell></TableRow>))}</TableBody></Table></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 35. CHANGELOG SCREEN
// ═══════════════════════════════════════════
function ChangelogScreen() {
  const entries = [
    { version: 'v2.1.0', date: 'Jun 8, 2026', type: 'feature', title: 'GxP Validated Mode', desc: 'Full GxP validated mode for clinical research with audit trails and electronic signatures.' },
    { version: 'v2.0.5', date: 'Jun 1, 2026', type: 'improvement', title: 'Knowledge Graph V2', desc: 'Updated knowledge graph engine with 50% faster query performance and 200K additional nodes.' },
    { version: 'v2.0.4', date: 'May 25, 2026', type: 'bugfix', title: 'Report Generation Fix', desc: 'Fixed issue where PDF reports occasionally had missing pathway diagrams.' },
    { version: 'v2.0.3', date: 'May 18, 2026', type: 'feature', title: 'Batch Query API', desc: 'New batch query endpoint allows up to 50 disease queries in a single API call.' },
    { version: 'v2.0.2', date: 'May 10, 2026', type: 'improvement', title: 'Safety Scoring Update', desc: 'Improved safety scoring algorithm with better off-target prediction accuracy.' },
  ];
  const typeColors: Record<string, string> = { feature: 'bg-blue-100 text-blue-700', improvement: 'bg-amber-100 text-amber-700', bugfix: 'bg-red-100 text-red-700' };
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Changelog" desc="Product updates and release notes" actions={<Button variant="outline" size="sm"><Bell className="h-4 w-4 mr-1.5" />Subscribe</Button>} />
      <div className="space-y-4">{entries.map(e => (<Card key={e.version + e.title} className="hover:shadow-md transition-shadow"><CardContent className="p-5"><div className="flex items-start justify-between mb-2"><div className="flex items-center gap-3"><Badge variant="outline" className="font-mono">{e.version}</Badge><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${typeColors[e.type]}`}>{e.type}</span></div><span className="text-xs text-muted-foreground">{e.date}</span></div><h3 className="font-semibold">{e.title}</h3><p className="text-sm text-muted-foreground mt-1">{e.desc}</p></CardContent></Card>))}</div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 36. ROADMAP SCREEN
// ═══════════════════════════════════════════
function RoadmapScreen() {
  const items = [
    { title: 'Multi-disease batch analysis', status: 'shipped', quarter: 'Q2 2026', votes: 124 },
    { title: 'Real-time collaboration', status: 'in-progress', quarter: 'Q3 2026', votes: 98 },
    { title: 'Dark mode theme', status: 'planned', quarter: 'Q3 2026', votes: 76 },
    { title: 'Advanced ADMET predictions', status: 'in-progress', quarter: 'Q3 2026', votes: 156 },
    { title: 'Custom knowledge graph views', status: 'planned', quarter: 'Q4 2026', votes: 89 },
    { title: 'Mobile app', status: 'planned', quarter: 'Q4 2026', votes: 203 },
    { title: 'Regulatory submission package', status: 'planned', quarter: 'Q1 2027', votes: 145 },
  ];
  const statusColors: Record<string, string> = { shipped: 'bg-green-100 text-green-700', 'in-progress': 'bg-blue-100 text-blue-700', planned: 'bg-slate-100 text-slate-700' };
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Product Roadmap" desc="Upcoming features and improvements" />
      <div className="flex gap-2 mb-2">{['all', 'shipped', 'in-progress', 'planned'].map(s => (<Badge key={s} variant="outline" className="cursor-pointer capitalize">{s}</Badge>))}</div>
      <div className="space-y-4">{items.map(item => (<Card key={item.title} className="hover:shadow-md transition-shadow"><CardContent className="p-5"><div className="flex items-start justify-between"><div><div className="flex items-center gap-3 mb-2"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${statusColors[item.status]}`}>{item.status}</span><Badge variant="outline" className="text-xs">{item.quarter}</Badge></div><h3 className="font-semibold">{item.title}</h3></div><div className="flex items-center gap-1.5"><ArrowUpRight className="h-4 w-4 text-muted-foreground" /><span className="text-sm font-medium">{item.votes}</span></div></div></CardContent></Card>))}</div>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// 37. FEEDBACK SCREEN
// ═══════════════════════════════════════════
function FeedbackScreen() {
  const [rating, setRating] = useState(0);
  const [category, setCategory] = useState('');
  const [description, setDescription] = useState('');
  const recentFeedback = [
    { user: 'Dr. Sarah Chen', rating: 5, category: 'Feature Request', feedback: 'Would love to see a dark mode option for late-night research sessions.', date: '2 days ago' },
    { user: 'James Wilson', rating: 4, category: 'Improvement', feedback: 'Export functionality is great but would be better with custom formatting options.', date: '5 days ago' },
    { user: 'Dr. Priya Patel', rating: 5, category: 'Praise', feedback: 'The knowledge graph visualization is incredibly helpful for understanding drug-disease relationships.', date: '1 week ago' },
  ];
  return (
    <FadeIn><div className="space-y-6">
      <PageHeader title="Feedback" desc="Help us improve DrugOS" />
      <Card><CardContent className="p-6 space-y-4">
        <div><Label>How would you rate your experience?</Label><div className="flex gap-2 mt-2">{[1,2,3,4,5].map(s => (<button key={s} onClick={() => setRating(s)} className={`text-2xl transition-colors ${s <= rating ? 'text-yellow-400' : 'text-muted-foreground/30'}`}>★</button>))}</div></div>
        <div><Label>Category</Label><Select value={category} onValueChange={setCategory}><SelectTrigger><SelectValue placeholder="Select category" /></SelectTrigger><SelectContent><SelectItem value="bug">Bug Report</SelectItem><SelectItem value="feature">Feature Request</SelectItem><SelectItem value="improvement">Improvement</SelectItem><SelectItem value="praise">Praise</SelectItem></SelectContent></Select></div>
        <div><Label>Description</Label><Textarea value={description} onChange={e => setDescription(e.target.value)} placeholder="Tell us more about your experience..." className="min-h-[100px]" /></div>
        <Button style={{ backgroundColor: PRIMARY }}><Send className="h-4 w-4 mr-1.5" />Submit Feedback</Button>
      </CardContent></Card>
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Recent Feedback</CardTitle></CardHeader><CardContent><div className="space-y-4">{recentFeedback.map(f => (<div key={f.user + f.date} className="p-4 border rounded-lg"><div className="flex items-center justify-between mb-2"><div className="flex items-center gap-2"><span className="font-medium text-sm">{f.user}</span><Badge variant="outline" className="text-xs">{f.category}</Badge></div><span className="text-xs text-muted-foreground">{f.date}</span></div><div className="flex gap-0.5 mb-2">{[1,2,3,4,5].map(s => (<span key={s} className={`text-sm ${s <= f.rating ? 'text-yellow-400' : 'text-muted-foreground/20'}`}>★</span>))}</div><p className="text-sm text-muted-foreground">{f.feedback}</p></div>))}</div></CardContent></Card>
    </div></FadeIn>
  );
}

// ═══════════════════════════════════════════
// EXPORT ALL REMAINING SCREENS
// ═══════════════════════════════════════════
export const remainingScreens: Record<string, React.ComponentType> = {
  'pipeline': PipelineScreen,
  'analytics': AnalyticsScreen,
  'team': TeamMembersScreen,
  'projects': ProjectsScreen,
  'shared-queries': SharedQueriesScreen,
  'annotations': AnnotationsScreen,
  'data-sources': DataSourcesScreen,
  'graph-stats': GraphStatisticsScreen,
  'quality': QualityScreen,
  'subscription': SubscriptionScreen,
  'usage': UsageScreen,
  'deals': DealsScreen,
  'invoices': InvoicesScreen,
  'users': UsersAdminScreen,
  'roles': RolesScreen,
  'sso': SSOScreen,
  'audit-logs': AuditLogsScreen,
  'feature-flags': FeatureFlagsScreen,
  'api-docs': APIDocsScreen,
  'api-keys': APIKeysScreen,
  'playground': PlaygroundScreen,
  'webhooks': WebhooksScreen,
  'profile': ProfileScreen,
  'security': SecuritySettingsScreen,
  'notifications': NotificationsScreen,
  'preferences': PreferencesScreen,
  'privacy': PrivacyPolicyScreen,
  'terms': TermsScreen,
  'compliance': ComplianceScreen,
  'help-center': HelpCenterScreen,
  'tickets': TicketScreen,
  'system-status': SystemStatusScreen,
  'investor-dashboard': InvestorDashboardScreen,
  'cap-table': CapTableScreen,
  'changelog': ChangelogScreen,
  'roadmap': RoadmapScreen,
  'feedback': FeedbackScreen,
};

