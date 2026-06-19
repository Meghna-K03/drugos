'use client';

import { useState, useMemo } from 'react';
import { useDrugOSNav } from './nav-context';
import {
  CreditCard, TrendingUp, Shield, Scale, Code, Settings, HelpCircle,
  FileQuestion, Users, CheckCircle, AlertTriangle, Clock, Download,
  Plus, Search, Filter, X, ChevronRight, ChevronDown, ExternalLink,
  Key, Globe, Lock, Eye, Mail, Bell, Zap, RefreshCw, Copy, Trash2,
  Edit, MoreVertical, ArrowUpRight, ArrowDownRight, DollarSign,
  BarChart3, PieChart, Target, BookOpen, MessageSquare, Send,
  FileText, Database, Server, Activity, Info, Save, Upload,
  ChevronLeft, Check, Minus, PlusCircle, AlertCircle, ShieldCheck,
  Building, UserCog, MapPin, Palette, Plug, BellRing, ScrollText,
  Calculator, Gauge, Workflow, Percent, ShoppingCart, Sitemap,
  ToggleRight, FolderLock, KeyRound, ScanSearch, Cookie, Archive,
  BookMarked, Play, Webhook, Braces, Upload as UploadIcon,
  Settings2, User, Smartphone, Headphones, Ticket, Library,
  MessageCircle, Lightbulb, RotateCcw, Heart as HeartIcon, ShieldAlert,
  LogIn, UserCheck, Hourglass, Siren
} from 'lucide-react';
import {
  Card, CardContent, CardHeader, CardTitle, CardDescription, CardFooter,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Slider } from '@/components/ui/slider';
import { Separator } from '@/components/ui/separator';
import { Textarea } from '@/components/ui/textarea';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle, DialogTrigger,
} from '@/components/ui/dialog';
import {
  Accordion, AccordionContent, AccordionItem, AccordionTrigger,
} from '@/components/ui/accordion';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area, RadarChart,
  Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip,
  ResponsiveContainer, Legend, PieChart as RechartsPieChart, Pie, Cell,
} from 'recharts';
import {
  users, auditLogs, subscriptionPlans, billingHistory,
  apiKeys, webhooks, usageMetrics, dealPipeline, organization,
  featureFlags, dataSources,
} from '@/lib/mock-data';

// ─── Design Tokens ───
const C = { primary: '#5B4FCF', green: '#1D9E75', orange: '#D4853A', red: '#C0392B', bg: '#F8F8FA' };

// ─── Shared Helpers ───
function PageHeader({ title, desc, actions }: { title: string; desc?: string; actions?: React.ReactNode }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-6">
      <div><h1 className="text-2xl font-bold text-foreground">{title}</h1>{desc && <p className="text-sm text-muted-foreground mt-1">{desc}</p>}</div>
      {actions && <div className="flex items-center gap-2 flex-shrink-0">{actions}</div>}
    </div>
  );
}

function StatCard({ title, value, subtitle, icon: Icon, trend }: { title: string; value: string | number; subtitle?: string; icon?: React.ComponentType<{className?:string}>; trend?: string }) {
  return (
    <Card className="hover:shadow-md transition-shadow">
      <CardContent className="p-5">
        <div className="flex items-start justify-between">
          <div>
            <p className="text-sm text-muted-foreground">{title}</p>
            <p className="text-2xl font-bold text-foreground mt-1">{value}</p>
            {subtitle && <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>}
            {trend && <p className={`text-xs mt-1 font-medium ${trend.startsWith('+') ? 'text-emerald-600' : 'text-red-500'}`}>{trend}</p>}
          </div>
          {Icon && <div className="h-10 w-10 rounded-lg bg-primary/10 flex items-center justify-center"><Icon className="h-5 w-5 text-primary" /></div>}
        </div>
      </CardContent>
    </Card>
  );
}

function ColorProgress({ value, max, label }: { value: number; max: number; label: string }) {
  const pct = Math.min((value / max) * 100, 100);
  const color = pct > 90 ? C.red : pct > 75 ? C.orange : pct > 50 ? '#EAB308' : C.green;
  return (
    <div className="space-y-1.5">
      <div className="flex justify-between text-sm"><span className="text-muted-foreground">{label}</span><span className="font-medium">{value.toLocaleString()}/{max.toLocaleString()}</span></div>
      <div className="h-2 w-full bg-muted rounded-full overflow-hidden"><div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, backgroundColor: color }} /></div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, 'default' | 'secondary' | 'outline' | 'destructive'> = {
    active: 'default', operational: 'default', healthy: 'default', Paid: 'default',
    inactive: 'secondary', degraded: 'secondary', pending: 'outline', invited: 'outline',
    suspended: 'destructive', error: 'destructive',
  };
  return <Badge variant={map[status] ?? 'outline'}>{status}</Badge>;
}

// ─── Chart Data ───
const usageTrendData = [
  { month: 'Jan', queries: 180, api: 22000, compute: 80 },
  { month: 'Feb', queries: 220, api: 28000, compute: 95 },
  { month: 'Mar', queries: 290, api: 35000, compute: 110 },
  { month: 'Apr', queries: 310, api: 38000, compute: 115 },
  { month: 'May', queries: 340, api: 42000, compute: 122 },
  { month: 'Jun', queries: 342, api: 45230, compute: 128 },
];

const endpointData = [
  { name: '/v1/query', calls: 18420, errors: 12 },
  { name: '/v1/candidates', calls: 12350, errors: 5 },
  { name: '/v1/explain', calls: 8200, errors: 3 },
  { name: '/v1/safety', calls: 4100, errors: 2 },
  { name: '/v1/report', calls: 2160, errors: 1 },
];

const revenueProjectionData = [
  { year: '2026', revenue: 12, expense: 18, ebitda: -6 },
  { year: '2027', revenue: 35, expense: 28, ebitda: 7 },
  { year: '2028', revenue: 85, expense: 42, ebitda: 43 },
  { year: '2029', revenue: 180, expense: 65, ebitda: 115 },
  { year: '2030', revenue: 350, expense: 95, ebitda: 255 },
];

const marketSizingData = [
  { name: 'TAM', value: 50, fill: C.primary },
  { name: 'SAM', value: 15, fill: C.green },
  { name: 'SOM', value: 3, fill: C.orange },
];

const radarData = [
  { subject: 'KG Coverage', DrugOS: 95, BenevolentAI: 72, Recursion: 60, OpenTargets: 80 },
  { subject: 'Explainability', DrugOS: 90, BenevolentAI: 65, Recursion: 45, OpenTargets: 55 },
  { subject: 'Safety Profiling', DrugOS: 88, BenevolentAI: 70, Recursion: 55, OpenTargets: 60 },
  { subject: 'API Quality', DrugOS: 92, BenevolentAI: 60, Recursion: 70, OpenTargets: 85 },
  { subject: 'Data Freshness', DrugOS: 85, BenevolentAI: 75, Recursion: 65, OpenTargets: 90 },
  { subject: 'Clinical Evidence', DrugOS: 82, BenevolentAI: 78, Recursion: 50, OpenTargets: 88 },
];

const comparableData = [
  { name: 'DrugOS', rev: 12, growth: 190, ev: 180, multiple: 15 },
  { name: 'BenevolentAI', rev: 45, growth: 35, ev: 600, multiple: 13 },
  { name: 'Recursion', rev: 80, growth: 55, ev: 2400, multiple: 30 },
  { name: 'Insilico', rev: 30, growth: 120, ev: 450, multiple: 15 },
  { name: 'Schrodinger', rev: 220, growth: 25, ev: 3300, multiple: 15 },
];

const pipelinePredictData = [
  { name: 'Preclinical', count: 24, successRate: 15 },
  { name: 'Phase I', count: 12, successRate: 30 },
  { name: 'Phase II', count: 8, successRate: 45 },
  { name: 'Phase III', count: 4, successRate: 65 },
  { name: 'Approved', count: 2, successRate: 90 },
];

const royaltyData = [
  { volume: '$0-50M', rate: 8, projected: 4 },
  { volume: '$50-100M', rate: 6, projected: 3 },
  { volume: '$100-250M', rate: 4, projected: 6 },
  { volume: '$250M+', rate: 2, projected: 5 },
];

const apiUsageTimeData = [
  { hour: '00:00', calls: 120 }, { hour: '04:00', calls: 80 },
  { hour: '08:00', calls: 450 }, { hour: '12:00', calls: 890 },
  { hour: '16:00', calls: 720 }, { hour: '20:00', calls: 340 },
  { hour: '23:59', calls: 180 },
];

const moatData = [
  { category: 'Data Volume', score: 92 },
  { category: 'Prediction Accuracy', score: 88 },
  { category: 'Validation Feedback', score: 85 },
  { category: 'Network Effects', score: 78 },
  { category: 'Switching Cost', score: 82 },
  { category: 'IP Protection', score: 75 },
];

