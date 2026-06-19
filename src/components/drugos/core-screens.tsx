'use client';

import { remainingScreens } from './remaining-screens';
import { useState, useMemo, useCallback, useEffect } from 'react';
import {
  Search, Download, ChevronDown, ChevronUp, Star, ArrowLeft,
  ShieldCheck, AlertTriangle, FlaskConical, FileBarChart, Package,
  Filter, CheckCircle2, XCircle, Clock, TrendingUp, BookOpen,
  GitBranch, BarChart3, FileText, Layers, Target, Activity,
  Zap, Database, Globe, ChevronRight, Plus, Minus, Eye,
  BookmarkPlus, Share2, ExternalLink, Info, AlertCircle,
  PieChart, LineChart, ClipboardCheck, Scale, Beaker,
  Atom, Hash, Calendar, Users, ArrowRight, Maximize2,
  RotateCcw, ZoomIn, ZoomOut, GripVertical, Trash2, Play,
  FileUp, Send, Sparkles, Brain, Timer, CheckSquare,
  Square, CircleDot, HelpCircle, Settings, RefreshCw,
} from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { Slider } from '@/components/ui/slider';
import { Separator } from '@/components/ui/separator';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {
  Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger,
} from '@/components/ui/sheet';
import {
  Collapsible, CollapsibleContent, CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar,
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip,
  PieChart as RechartsPie, Pie, Cell, ResponsiveContainer, Legend,
  LineChart as RechartsLine, Line,
} from 'recharts';
import { motion, AnimatePresence } from 'framer-motion';
import { useDrugOSNav } from './nav-context';
import {
  diseases, drugCandidates, clinicalTrials, graphNodes, graphEdges,
  trendingDiseases, recentQueries, savedQueries, usageMetrics,
  patents, evidenceItems, admetProfiles, offTargetPredictions,
  drugInteractions,
  type DrugCandidate, type Disease, type ClinicalTrial,
  type GraphNode, type GraphEdge, type Patent, type EvidenceItem,
  type ADMETProfile, type OffTargetPrediction, type DrugInteraction,
} from '@/lib/mock-data';
import { api, type MeshResult, type ClinicalTrial as APIClinicalTrial, type PubMedArticle, type OpenFdaAdverseEvent, type PatentResult, type EvidencePackage, errorMessage } from '@/lib/api-client';

// ═══════════════════════════════════════════
// SHARED HELPERS
// ═══════════════════════════════════════════

const PRIMARY = '#5B4FCF';
const ACCENT_GREEN = '#1D9E75';
const ACCENT_ORANGE = '#D4853A';
const ACCENT_RED = '#C0392B';
const BG = '#F8F8FA';

function scoreColor(s: number) {
  if (s >= 80) return ACCENT_GREEN;
  if (s >= 60) return ACCENT_ORANGE;
  return ACCENT_RED;
}

function ScoreBar({ score, size = 'md' }: { score: number; size?: 'sm' | 'md' | 'lg' }) {
  const color = scoreColor(score);
  const h = size === 'sm' ? 'h-1.5' : size === 'lg' ? 'h-3.5' : 'h-2.5';
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-bold" style={{ color }}>{score}</span>
      <div className="flex-1 bg-slate-100 rounded-full overflow-hidden">
        <div className={`${h} rounded-full transition-all duration-500`} style={{ width: `${score}%`, backgroundColor: color }} />
      </div>
    </div>
  );
}

function SafetyBadge({ tier }: { tier: 'green' | 'yellow' | 'red' }) {
  const cfg = {
    green: { label: 'Safe', bg: 'bg-emerald-50', text: 'text-emerald-700', border: 'border-emerald-200', dot: 'bg-emerald-500' },
    yellow: { label: 'Caution', bg: 'bg-amber-50', text: 'text-amber-700', border: 'border-amber-200', dot: 'bg-amber-500' },
    red: { label: 'High Risk', bg: 'bg-red-50', text: 'text-red-700', border: 'border-red-200', dot: 'bg-red-500' },
  };
  const c = cfg[tier];
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-semibold border ${c.bg} ${c.text} ${c.border}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${c.dot}`} />
      {c.label}
    </span>
  );
}

function StatCard({ icon: Icon, value, label, color = PRIMARY }: { icon: React.ElementType; value: string | number; label: string; color?: string }) {
  return (
    <Card className="hover:shadow-md transition-shadow">
      <CardContent className="p-4">
        <div className="flex items-start justify-between">
          <div>
            <p className="text-sm text-muted-foreground">{label}</p>
            <p className="text-2xl font-bold mt-1">{value}</p>
          </div>
          <div className="rounded-lg p-2.5" style={{ backgroundColor: `${color}15` }}>
            <Icon className="h-5 w-5" style={{ color }} />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function PageHeader({ title, description, actions, onBack }: { title: string; description?: string; actions?: React.ReactNode; onBack?: () => void }) {
  const { navigate } = useDrugOSNav();
  return (
    <div className="flex items-start justify-between mb-6">
      <div className="flex items-start gap-3">
        {onBack && (
          <Button variant="ghost" size="sm" onClick={onBack} className="mt-0.5 h-8 w-8 p-0">
            <ArrowLeft className="h-4 w-4" />
          </Button>
        )}
        <div>
          <h1 className="text-2xl font-bold text-foreground">{title}</h1>
          {description && <p className="text-sm text-muted-foreground mt-0.5">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

function FadeIn({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3, delay }}>
      {children}
    </motion.div>
  );
}

// ═══════════════════════════════════════════
// 1. DISEASE SEARCH SCREEN
// ═══════════════════════════════════════════

function DiseaseSearchScreen() {
  const { navigate } = useDrugOSNav();
  const [query, setQuery] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [therapeuticArea, setTherapeuticArea] = useState('all');
  const [geneticOnly, setGeneticOnly] = useState(false);
  const [liveResults, setLiveResults] = useState<MeshResult[]>([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);

  // Debounced live MeSH search via /api/diseases/search.
  useEffect(() => {
    if (query.trim().length < 2) {
      setLiveResults([]);
      setLiveError(null);
      return;
    }
    let cancelled = false;
    setLiveLoading(true);
    setLiveError(null);
    const t = setTimeout(async () => {
      try {
        const res = await api.diseases.search(query.trim(), 8);
        if (!cancelled) {
          setLiveResults(res.results || []);
        }
      } catch (e) {
        if (!cancelled) setLiveError(errorMessage(e));
      } finally {
        if (!cancelled) setLiveLoading(false);
      }
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [query]);

  const suggestions = useMemo(() => {
    if (query.length < 2) return [];
    // Use real MeSH results from the backend, falling back to local
    // mock data so the demo still flows when the network is slow.
    if (liveResults.length > 0) {
      return liveResults.map(r => ({
        id: r.descriptorUi,
        name: r.label,
        icdCode: '',
        meshTerm: r.descriptorUi,
        therapeuticArea: r.scope || 'MeSH descriptor',
        prevalence: '',
        description: r.scope || '',
        geneticBasis: false,
      }));
    }
    const q = query.toLowerCase();
    return diseases.filter(d =>
      d.name.toLowerCase().includes(q) ||
      d.icdCode.toLowerCase().includes(q) ||
      d.meshTerm.toLowerCase().includes(q) ||
      d.therapeuticArea.toLowerCase().includes(q)
    ).slice(0, 8);
  }, [query, liveResults]);

  const filteredTrending = useMemo(() => {
    let items = trendingDiseases;
    if (therapeuticArea !== 'all') {
      const areaDiseases = diseases.filter(d => d.therapeuticArea === therapeuticArea).map(d => d.name);
      items = items.filter(t => areaDiseases.some(ad => t.name.includes(ad.split(' ')[0])));
    }
    return items;
  }, [therapeuticArea]);

  const handleSelectDisease = (diseaseId: string) => {
    navigate({ page: 'app', section: 'results', id: diseaseId });
  };

  const handleSearch = () => {
    if (query.trim()) {
      const match = diseases.find(d => d.name.toLowerCase().includes(query.toLowerCase()));
      if (match) {
        navigate({ page: 'app', section: 'results', id: match.id });
      } else if (liveResults[0]) {
        // No local mock match — use the first live MeSH result.
        navigate({ page: 'app', section: 'results', id: liveResults[0].descriptorUi });
      }
    }
  };

  const quickStartTemplates = [
    { name: "Huntington's Disease", id: 'D001', icon: '🧬' },
    { name: "Alzheimer's Disease", id: 'D002', icon: '🧠' },
    { name: 'Pancreatic Cancer', id: 'D006', icon: '🎯' },
  ];

  const therapeuticAreas = [...new Set(diseases.map(d => d.therapeuticArea))];

  return (
    <FadeIn>
      <div className="max-w-4xl mx-auto">
        {/* Hero Search */}
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold mb-2">Find Drug Repurposing Candidates</h1>
          <p className="text-muted-foreground mb-6">Search for a disease to discover ranked drug candidates powered by AI</p>
          <div className="relative max-w-2xl mx-auto">
            <Search className="absolute left-4 top-1/2 -translate-y-1/2 h-5 w-5 text-muted-foreground" />
            <Input
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              placeholder="Search diseases, ICD codes, genes, pathways..."
              className="pl-12 pr-28 h-12 text-base border-2 border-primary/20 focus:border-primary rounded-xl shadow-lg shadow-primary/5"
            />
            <Button onClick={handleSearch} className="absolute right-1.5 top-1.5 h-9 px-5 rounded-lg" style={{ backgroundColor: PRIMARY }}>
              Search
            </Button>
            {/* Autocomplete dropdown — live MeSH results */}
            {(suggestions.length > 0 || liveLoading || liveError) && (
              <div className="absolute z-50 w-full mt-1 bg-popover border border-border rounded-xl shadow-xl overflow-hidden">
                {liveLoading && (
                  <div className="px-4 py-2.5 text-sm text-muted-foreground flex items-center gap-2">
                    <RefreshCw className="w-3.5 h-3.5 animate-spin" /> Searching MeSH…
                  </div>
                )}
                {liveError && !liveLoading && (
                  <div className="px-4 py-2.5 text-sm text-amber-700 bg-amber-50">
                    Live MeSH search unavailable: {liveError}. Showing local matches.
                  </div>
                )}
                {suggestions.map(d => (
                  <button
                    key={d.id}
                    onClick={() => handleSelectDisease(d.id)}
                    className="flex items-center justify-between w-full px-4 py-2.5 text-sm hover:bg-accent text-left transition-colors"
                  >
                    <div>
                      <span className="font-medium">{d.name}</span>
                      <span className="ml-2 text-xs text-muted-foreground">{d.therapeuticArea}</span>
                    </div>
                    <Badge variant="secondary" className="text-xs font-mono">{d.icdCode || d.meshTerm}</Badge>
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="flex items-center justify-center gap-2 mt-3">
            <span className="text-xs text-muted-foreground">{usageMetrics.queries.used}/{usageMetrics.queries.limit} queries used this period</span>
            <Progress value={usageMetrics.queries.used} max={usageMetrics.queries.limit} />
          </div>
        </div>

        {/* Quick Start Templates */}
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-muted-foreground mb-3">Quick Start</h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {quickStartTemplates.map(t => (
              <Card key={t.id} className="cursor-pointer hover:shadow-md hover:border-primary/30 transition-all" onClick={() => handleSelectDisease(t.id)}>
                <CardContent className="p-4 flex items-center gap-3">
                  <span className="text-2xl">{t.icon}</span>
                  <span className="font-medium text-sm">{t.name}</span>
                  <ChevronRight className="h-4 w-4 text-muted-foreground ml-auto" />
                </CardContent>
              </Card>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Recent Queries */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <Clock className="h-4 w-4 text-muted-foreground" /> Recent Queries
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {recentQueries.map(q => {
                const disease = diseases.find(d => d.name === q.disease);
                return (
                  <button
                    key={q.id}
                    onClick={() => disease && handleSelectDisease(disease.id)}
                    className="flex items-center justify-between w-full p-2.5 rounded-lg hover:bg-accent text-left text-sm transition-colors"
                  >
                    <div>
                      <span className="font-medium">{q.disease}</span>
                      <span className="text-xs text-muted-foreground ml-2">{q.date}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge variant="secondary" className="text-xs">{q.candidates} candidates</Badge>
                      <span className="text-xs font-bold" style={{ color: scoreColor(q.topScore) }}>{q.topScore}</span>
                    </div>
                  </button>
                );
              })}
            </CardContent>
          </Card>

          {/* Trending Diseases */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <TrendingUp className="h-4 w-4 text-muted-foreground" /> Trending Diseases
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {filteredTrending.map((t, i) => {
                const disease = diseases.find(d => d.name === t.name || d.name.includes(t.name.split(' ')[0]));
                return (
                  <button
                    key={i}
                    onClick={() => disease && handleSelectDisease(disease.id)}
                    className="flex items-center justify-between w-full p-2.5 rounded-lg hover:bg-accent text-left text-sm transition-colors"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{t.name}</span>
                      <span className="text-xs text-emerald-600 flex items-center gap-0.5">
                        <TrendingUp className="h-3 w-3" />+{t.change}%
                      </span>
                    </div>
                    <Badge variant="outline" className="text-xs">{t.queries} queries</Badge>
                  </button>
                );
              })}
            </CardContent>
          </Card>
        </div>

        {/* Advanced Search */}
        <Collapsible open={showAdvanced} onOpenChange={setShowAdvanced} className="mt-6">
          <CollapsibleTrigger asChild>
            <Button variant="outline" className="w-full">
              <Filter className="h-4 w-4 mr-2" />
              {showAdvanced ? 'Hide' : 'Show'} Advanced Search
              <ChevronDown className={`h-4 w-4 ml-auto transition-transform ${showAdvanced ? 'rotate-180' : ''}`} />
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-4">
            <Card>
              <CardContent className="p-6 space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                  <div>
                    <label className="text-sm font-medium mb-1.5 block">Therapeutic Area</label>
                    <Select value={therapeuticArea} onValueChange={setTherapeuticArea}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="all">All Areas</SelectItem>
                        {therapeuticAreas.map(a => <SelectItem key={a} value={a}>{a}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <label className="text-sm font-medium mb-1.5 block">Prevalence</label>
                    <Select defaultValue="all">
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="all">Any</SelectItem>
                        <SelectItem value="rare">Rare (&lt;1/2000)</SelectItem>
                        <SelectItem value="common">Common</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex items-end gap-2 pb-1">
                    <Checkbox id="genetic" checked={geneticOnly} onCheckedChange={v => setGeneticOnly(!!v)} />
                    <label htmlFor="genetic" className="text-sm font-medium">Genetic basis only</label>
                  </div>
                </div>
                <Button className="w-full" style={{ backgroundColor: PRIMARY }} onClick={handleSearch}>
                  <Search className="h-4 w-4 mr-2" /> Search with Filters
                </Button>
              </CardContent>
            </Card>
          </CollapsibleContent>
        </Collapsible>

        {/* Browse All Diseases */}
        <div className="mt-6">
          <h3 className="text-sm font-semibold text-muted-foreground mb-3">Browse All Diseases</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {diseases
              .filter(d => therapeuticArea === 'all' || d.therapeuticArea === therapeuticArea)
              .filter(d => !geneticOnly || d.geneticBasis)
              .map(d => (
                <Card key={d.id} className="cursor-pointer hover:shadow-md hover:border-primary/30 transition-all" onClick={() => handleSelectDisease(d.id)}>
                  <CardContent className="p-4">
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="font-medium text-sm">{d.name}</h4>
                      <Badge variant="secondary" className="text-[10px] font-mono">{d.icdCode}</Badge>
                    </div>
                    <p className="text-xs text-muted-foreground line-clamp-2">{d.description}</p>
                    <div className="flex items-center gap-2 mt-2">
                      <Badge variant="outline" className="text-[10px]">{d.therapeuticArea}</Badge>
                      <span className="text-[10px] text-muted-foreground">{d.prevalence}</span>
                    </div>
                  </CardContent>
                </Card>
              ))}
          </div>
        </div>
      </div>
    </FadeIn>
  );
}

function Progress({ value, max }: { value: number; max: number }) {
  const pct = Math.min((value / max) * 100, 100);
  const color = pct > 90 ? ACCENT_RED : pct > 75 ? ACCENT_ORANGE : PRIMARY;
  return (
    <div className="w-20 h-1.5 bg-slate-100 rounded-full overflow-hidden">
      <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, backgroundColor: color }} />
    </div>
  );
}

// ═══════════════════════════════════════════
// 2. SEARCH RESULTS SCREEN
// ═══════════════════════════════════════════

function SearchResultsScreen() {
  const { navigate, currentRoute } = useDrugOSNav();
  const diseaseId = currentRoute.id || 'D001';
  const disease = diseases.find(d => d.id === diseaseId) || diseases[0];
  const candidates = drugCandidates.filter(c => c.diseaseId === diseaseId);

  const [filterTier, setFilterTier] = useState<string>('all');
  const [filterPhase, setFilterPhase] = useState<string>('all');
  const [sortKey, setSortKey] = useState<string>('compositeScore');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [shortlisted, setShortlisted] = useState<Set<string>>(new Set());
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [scoreRange, setScoreRange] = useState<[number, number]>([0, 100]);

  const filtered = useMemo(() => {
    let items = [...candidates];
    if (filterTier !== 'all') items = items.filter(c => c.safetyTier === filterTier);
    if (filterPhase !== 'all') items = items.filter(c => c.clinicalPhase === filterPhase);
    items = items.filter(c => c.compositeScore >= scoreRange[0] && c.compositeScore <= scoreRange[1]);
    items.sort((a, b) => {
      const aVal = (a as Record<string, unknown>)[sortKey] as number;
      const bVal = (b as Record<string, unknown>)[sortKey] as number;
      return sortDir === 'desc' ? bVal - aVal : aVal - bVal;
    });
    return items;
  }, [candidates, filterTier, filterPhase, sortKey, sortDir, scoreRange]);

  const handleSort = (key: string) => {
    if (sortKey === key) setSortDir(d => d === 'desc' ? 'asc' : 'desc');
    else { setSortKey(key); setSortDir('desc'); }
  };

  const toggleShortlist = (id: string) => {
    setShortlisted(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const phases = [...new Set(candidates.map(c => c.clinicalPhase))];
  const renderSortIcon = (col: string) => sortKey === col ? (sortDir === 'desc' ? <ChevronDown className="h-3 w-3 ml-1" /> : <ChevronUp className="h-3 w-3 ml-1" />) : null;

  return (
    <FadeIn>
      <PageHeader
        title={disease.name}
        description={`${candidates.length} drug repurposing candidates found · ICD-10: ${disease.icdCode}`}
        onBack={() => navigate({ page: 'app', section: 'search' })}
        actions={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm">
              <Download className="h-4 w-4 mr-1.5" /> Export CSV
            </Button>
            {shortlisted.size > 0 && (
              <Button variant="outline" size="sm" onClick={() => navigate({ page: 'app', section: 'shortlists' })}>
                <BookmarkPlus className="h-4 w-4 mr-1.5" /> Shortlist ({shortlisted.size})
              </Button>
            )}
          </div>
        }
      />

      {/* Filter Bar */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <span className="text-xs font-medium text-muted-foreground mr-1">Safety:</span>
        {['all', 'green', 'yellow', 'red'].map(t => (
          <Badge key={t} variant={filterTier === t ? 'default' : 'outline'} className="cursor-pointer" onClick={() => setFilterTier(t)}>
            {t === 'all' ? 'All' : t === 'green' ? '🟢 Safe' : t === 'yellow' ? '🟡 Caution' : '🔴 Risk'}
          </Badge>
        ))}
        <Separator orientation="vertical" className="h-5 mx-1" />
        <span className="text-xs font-medium text-muted-foreground mr-1">Phase:</span>
        <Select value={filterPhase} onValueChange={setFilterPhase}>
          <SelectTrigger className="w-36 h-7 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Phases</SelectItem>
            {phases.map(p => <SelectItem key={p} value={p}>{p}</SelectItem>)}
          </SelectContent>
        </Select>
        <Separator orientation="vertical" className="h-5 mx-1" />
        <span className="text-xs font-medium text-muted-foreground">Score:</span>
        <Slider value={scoreRange} onValueChange={v => setScoreRange(v as [number, number])} min={0} max={100} step={5} className="w-28" />
        <span className="text-xs text-muted-foreground">{scoreRange[0]}–{scoreRange[1]}</span>
      </div>

      {/* Results Table */}
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow className="bg-muted/50 hover:bg-muted/50">
                <TableHead className="w-8">★</TableHead>
                <TableHead className="w-8">#</TableHead>
                <TableHead>Drug Name</TableHead>
                <TableHead className="cursor-pointer select-none" onClick={() => handleSort('compositeScore')}>
                  Composite Score {renderSortIcon('compositeScore')}
                </TableHead>
                <TableHead>Safety</TableHead>
                <TableHead>Mechanism</TableHead>
                <TableHead>Phase</TableHead>
                <TableHead>IP Status</TableHead>
                <TableHead className="w-8"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((c, i) => (
                <>
                  <TableRow key={c.id} className="cursor-pointer hover:bg-muted/30" onClick={() => navigate({ page: 'app', section: 'candidate', id: c.id })}>
                    <TableCell onClick={e => { e.stopPropagation(); toggleShortlist(c.id); }}>
                      <Star className={`h-4 w-4 ${shortlisted.has(c.id) ? 'fill-yellow-400 text-yellow-400' : 'text-muted-foreground hover:text-yellow-400'} transition-colors`} />
                    </TableCell>
                    <TableCell className="font-bold text-muted-foreground text-xs">{i + 1}</TableCell>
                    <TableCell>
                      <div>
                        <span className="font-medium text-sm">{c.drugName}</span>
                        <span className="text-xs text-muted-foreground ml-1.5">({c.brandNames.join(', ')})</span>
                      </div>
                    </TableCell>
                    <TableCell><ScoreBar score={c.compositeScore} size="sm" /></TableCell>
                    <TableCell><SafetyBadge tier={c.safetyTier} /></TableCell>
                    <TableCell><span className="text-xs text-slate-600 line-clamp-2 max-w-[180px]">{c.mechanism}</span></TableCell>
                    <TableCell><Badge variant="outline" className="text-xs">{c.clinicalPhase}</Badge></TableCell>
                    <TableCell><span className="text-xs">{c.ipStatus}</span></TableCell>
                    <TableCell>
                      <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={e => { e.stopPropagation(); setExpandedId(expandedId === c.id ? null : c.id); }}>
                        {expandedId === c.id ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                      </Button>
                    </TableCell>
                  </TableRow>
                  {expandedId === c.id && (
                    <TableRow key={`${c.id}-detail`}>
                      <TableCell colSpan={9} className="bg-muted/20 p-4">
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
                          <div><span className="text-muted-foreground">KG Score:</span> <span className="font-semibold">{c.kgScore}</span></div>
                          <div><span className="text-muted-foreground">Mol Similarity:</span> <span className="font-semibold">{c.molSimScore}</span></div>
                          <div><span className="text-muted-foreground">Safety Score:</span> <span className="font-semibold">{c.safetyScore}</span></div>
                          <div><span className="text-muted-foreground">Clinical Score:</span> <span className="font-semibold">{c.clinicalScore}</span></div>
                        </div>
                        <div className="mt-2">
                          <span className="text-xs text-muted-foreground">Targets: </span>
                          {c.targets.map(t => <Badge key={t} variant="secondary" className="text-xs mr-1">{t}</Badge>)}
                        </div>
                        <div className="mt-1">
                          <span className="text-xs text-muted-foreground">Pathways: </span>
                          {c.pathways.map(p => <Badge key={p} variant="outline" className="text-xs mr-1">{p}</Badge>)}
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </>
              ))}
            </TableBody>
          </Table>
          {filtered.length === 0 && (
            <div className="text-center py-12 text-muted-foreground">
              <Search className="h-8 w-8 mx-auto mb-2 opacity-50" />
              <p>No candidates match your filters</p>
            </div>
          )}
        </CardContent>
      </Card>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// 3. CANDIDATE DETAIL SCREEN
// ═══════════════════════════════════════════

function CandidateDetailScreen() {
  const { navigate, currentRoute } = useDrugOSNav();
  const candidateId = currentRoute.id || 'DC001';
  const candidate = drugCandidates.find(c => c.id === candidateId) || drugCandidates[0];
  const disease = diseases.find(d => d.id === candidate.diseaseId) || diseases[0];
  const [activeTab, setActiveTab] = useState('overview');

  const relatedTrials = clinicalTrials.filter(t => t.drugName === candidate.drugName);
  const relatedPatents = patents.filter(p => p.drugName === candidate.drugName);
  const relatedEvidence = evidenceItems.filter(e => e.drugName === candidate.drugName);
  const admet = admetProfiles.find(a => a.drugName === candidate.drugName);
  const offTargets = offTargetPredictions.filter(o => o.drugName === candidate.drugName);
  const interactions = drugInteractions.filter(d => d.drug1 === candidate.drugName);

  return (
    <FadeIn>
      <PageHeader
        title={candidate.drugName}
        description={`${candidate.genericName} · ${candidate.brandNames.join(', ')} · for ${disease.name}`}
        onBack={() => navigate({ page: 'app', section: 'results', id: candidate.diseaseId })}
        actions={
          <div className="flex items-center gap-2">
            <SafetyBadge tier={candidate.safetyTier} />
            <Badge variant="outline">{candidate.clinicalPhase}</Badge>
            <Badge variant="outline">{candidate.ipStatus}</Badge>
          </div>
        }
      />

      {/* Stat Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
        <StatCard icon={Activity} value={candidate.compositeScore} label="Composite Score" color={scoreColor(candidate.compositeScore)} />
        <StatCard icon={Database} value={candidate.kgScore} label="KG Score" color={PRIMARY} />
        <StatCard icon={ShieldCheck} value={candidate.safetyScore} label="Safety Score" color={ACCENT_GREEN} />
        <StatCard icon={FlaskConical} value={candidate.clinicalScore} label="Clinical Score" color={ACCENT_ORANGE} />
      </div>

      {/* Tabs */}
      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="w-full justify-start h-auto p-1 bg-muted/50 rounded-lg flex-wrap">
          {['overview', 'pathway', 'safety', 'clinical', 'ip', 'evidence'].map(tab => (
            <TabsTrigger key={tab} value={tab} className="capitalize gap-1.5 data-[state=active]:bg-background data-[state=active]:shadow-sm">
              {tab}
              {tab === 'clinical' && relatedTrials.length > 0 && <span className="ml-1 px-1.5 py-0.5 text-[10px] font-medium bg-primary/10 text-primary rounded-full">{relatedTrials.length}</span>}
              {tab === 'ip' && relatedPatents.length > 0 && <span className="ml-1 px-1.5 py-0.5 text-[10px] font-medium bg-primary/10 text-primary rounded-full">{relatedPatents.length}</span>}
              {tab === 'evidence' && relatedEvidence.length > 0 && <span className="ml-1 px-1.5 py-0.5 text-[10px] font-medium bg-primary/10 text-primary rounded-full">{relatedEvidence.length}</span>}
            </TabsTrigger>
          ))}
        </TabsList>

        {/* OVERVIEW TAB */}
        <TabsContent value="overview" className="mt-4">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2 space-y-4">
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Score Breakdown</CardTitle></CardHeader>
                <CardContent className="space-y-3">
                  {[
                    { label: 'Knowledge Graph Score', value: candidate.kgScore },
                    { label: 'Molecular Similarity', value: candidate.molSimScore },
                    { label: 'Safety Profile', value: candidate.safetyScore },
                    { label: 'Clinical Evidence', value: candidate.clinicalScore },
                  ].map(s => (
                    <div key={s.label}>
                      <div className="flex justify-between text-sm mb-1"><span className="text-muted-foreground">{s.label}</span><span className="font-semibold">{s.value}</span></div>
                      <div className="w-full bg-slate-100 rounded-full h-2.5 overflow-hidden">
                        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${s.value}%`, backgroundColor: scoreColor(s.value) }} />
                      </div>
                    </div>
                  ))}
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Mechanism of Action</CardTitle></CardHeader>
                <CardContent>
                  <p className="text-sm">{candidate.mechanism}</p>
                  <div className="mt-3">
                    <span className="text-xs font-medium text-muted-foreground">Target Proteins: </span>
                    {candidate.targets.map(t => <Badge key={t} variant="secondary" className="text-xs mr-1 font-mono">{t}</Badge>)}
                  </div>
                  <div className="mt-2">
                    <span className="text-xs font-medium text-muted-foreground">Pathways: </span>
                    {candidate.pathways.map(p => <Badge key={p} variant="outline" className="text-xs mr-1">{p}</Badge>)}
                  </div>
                </CardContent>
              </Card>
            </div>
            <div className="space-y-4">
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Key Evidence</CardTitle></CardHeader>
                <CardContent className="space-y-2">
                  {relatedEvidence.slice(0, 4).map(ev => (
                    <div key={ev.id} className="p-2.5 border rounded-lg text-sm">
                      <div className="flex items-center gap-2 mb-1">
                        <Badge variant="secondary" className="text-[10px]">{ev.type}</Badge>
                        <span className="font-medium text-xs">{ev.source}</span>
                      </div>
                      <p className="text-xs text-muted-foreground line-clamp-2">{ev.title}</p>
                    </div>
                  ))}
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Drug Info</CardTitle></CardHeader>
                <CardContent className="space-y-2 text-sm">
                  <div className="flex justify-between"><span className="text-muted-foreground">Generic</span><span className="font-medium">{candidate.genericName}</span></div>
                  <div className="flex justify-between"><span className="text-muted-foreground">Brand</span><span className="font-medium">{candidate.brandNames.join(', ')}</span></div>
                  <div className="flex justify-between"><span className="text-muted-foreground">Phase</span><Badge variant="outline" className="text-xs">{candidate.clinicalPhase}</Badge></div>
                  <div className="flex justify-between"><span className="text-muted-foreground">IP</span><Badge variant="outline" className="text-xs">{candidate.ipStatus}</Badge></div>
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>

        {/* PATHWAY TAB */}
        <TabsContent value="pathway" className="mt-4">
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Drug → Target → Pathway → Disease</CardTitle></CardHeader>
            <CardContent>
              <PathwayDiagram candidate={candidate} disease={disease} />
            </CardContent>
          </Card>
        </TabsContent>

        {/* SAFETY TAB */}
        <TabsContent value="safety" className="mt-4">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base">Safety Tier</CardTitle>
                  <SafetyBadge tier={candidate.safetyTier} />
                </div>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground mb-4">
                  {candidate.safetyTier === 'green' ? 'Low risk profile — suitable for repurposing investigation with standard monitoring.' :
                   candidate.safetyTier === 'yellow' ? 'Moderate risk — requires enhanced monitoring and risk mitigation strategies.' :
                   'High risk — significant safety concerns require careful benefit-risk assessment.'}
                </p>
                {admet && <ADMETRadarChart data={admet} />}
              </CardContent>
            </Card>
            <div className="space-y-4">
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Off-Target Predictions</CardTitle></CardHeader>
                <CardContent>
                  {offTargets.length > 0 ? (
                    <Table>
                      <TableHeader><TableRow><TableHead>Target</TableHead><TableHead>Probability</TableHead><TableHead>Severity</TableHead><TableHead>System</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {offTargets.map((o, i) => (
                          <TableRow key={i}>
                            <TableCell className="text-sm">{o.target}</TableCell>
                            <TableCell className="text-sm">{Math.round(o.probability * 100)}%</TableCell>
                            <TableCell><Badge variant={o.severity === 'high' ? 'destructive' : o.severity === 'medium' ? 'secondary' : 'outline'} className="text-xs">{o.severity}</Badge></TableCell>
                            <TableCell className="text-xs">{o.organSystem}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  ) : <p className="text-sm text-muted-foreground">No off-target predictions available</p>}
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Drug-Drug Interactions</CardTitle></CardHeader>
                <CardContent className="space-y-2">
                  {interactions.length > 0 ? interactions.map((int, i) => (
                    <div key={i} className="p-2.5 border rounded-lg">
                      <div className="flex items-center gap-2">
                        <Badge variant={int.severity === 'contraindicated' ? 'destructive' : int.severity === 'major' ? 'secondary' : 'outline'} className="text-xs">{int.severity}</Badge>
                        <span className="text-sm font-medium">{int.drug2}</span>
                      </div>
                      <p className="text-xs text-muted-foreground mt-1">{int.description} — {int.mechanism}</p>
                    </div>
                  )) : <p className="text-sm text-muted-foreground">No known interactions</p>}
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>

        {/* CLINICAL TAB */}
        <TabsContent value="clinical" className="mt-4">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2">
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Clinical Trials</CardTitle></CardHeader>
                <CardContent className="space-y-3">
                  {relatedTrials.length > 0 ? relatedTrials.map(trial => (
                    <Card key={trial.id} className="border">
                      <CardContent className="p-4">
                        <h4 className="font-medium text-sm">{trial.title}</h4>
                        <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                          <Badge variant="outline" className="text-xs font-mono">{trial.nctId}</Badge>
                          <Badge variant="secondary" className="text-xs">{trial.phase}</Badge>
                          <Badge className="text-xs">{trial.status}</Badge>
                        </div>
                        <p className="text-xs text-muted-foreground mt-2">Enrollment: {trial.enrollment} · {trial.startDate} – {trial.completionDate}</p>
                        {trial.outcome && <p className="text-xs mt-1"><span className="font-medium">Outcome:</span> {trial.outcome}</p>}
                      </CardContent>
                    </Card>
                  )) : <p className="text-sm text-muted-foreground">No clinical trials found</p>}
                </CardContent>
              </Card>
            </div>
            <div className="space-y-4">
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Phase Distribution</CardTitle></CardHeader>
                <CardContent>
                  <PhaseDistributionChart trials={relatedTrials} />
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Success Prediction</CardTitle></CardHeader>
                <CardContent>
                  <div className="text-center">
                    <div className="text-4xl font-bold" style={{ color: scoreColor(candidate.clinicalScore) }}>
                      {Math.round(candidate.clinicalScore * 0.6 + 15)}%
                    </div>
                    <p className="text-sm text-muted-foreground mt-1">Predicted trial success rate</p>
                    <Progress value={Math.round(candidate.clinicalScore * 0.6 + 15)} max={100} />
                  </div>
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>

        {/* IP TAB */}
        <TabsContent value="ip" className="mt-4">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2">
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Patent Status</CardTitle></CardHeader>
                <CardContent className="space-y-3">
                  {relatedPatents.length > 0 ? relatedPatents.map(pat => (
                    <div key={pat.id} className="p-4 border rounded-lg">
                      <div className="flex items-center justify-between mb-2">
                        <span className="font-medium text-sm">{pat.title}</span>
                        <Badge variant={pat.status === 'active' ? 'default' : pat.status === 'expired' ? 'secondary' : pat.status === 'pending' ? 'outline' : 'destructive'}>
                          {pat.status}
                        </Badge>
                      </div>
                      <div className="text-xs text-muted-foreground space-y-0.5">
                        <p>{pat.patentNumber} · {pat.jurisdiction} · {pat.claims} claims</p>
                        <p>Assignee: {pat.assignee}</p>
                        <p>Filed: {pat.filingDate} · Expires: {pat.expirationDate}</p>
                      </div>
                    </div>
                  )) : <p className="text-sm text-muted-foreground">No patents found for {candidate.drugName}</p>}
                </CardContent>
              </Card>
            </div>
            <div className="space-y-4">
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Freedom to Operate</CardTitle></CardHeader>
                <CardContent>
                  <div className="text-center">
                    <div className="text-3xl font-bold" style={{ color: candidate.ipStatus === 'Off-Patent' || candidate.ipStatus === 'Patent Expired' ? ACCENT_GREEN : candidate.ipStatus === 'Novel Use Patentable' ? ACCENT_ORANGE : ACCENT_RED }}>
                      {candidate.ipStatus === 'Off-Patent' || candidate.ipStatus === 'Patent Expired' ? 'Clear' : candidate.ipStatus === 'Novel Use Patentable' ? 'Partial' : 'Restricted'}
                    </div>
                    <p className="text-sm text-muted-foreground mt-1">IP Status: {candidate.ipStatus}</p>
                  </div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-3"><CardTitle className="text-base">Patent Timeline</CardTitle></CardHeader>
                <CardContent>
                  <PatentTimeline patents={relatedPatents} />
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>

        {/* EVIDENCE TAB */}
        <TabsContent value="evidence" className="mt-4">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2">
              <Card>
                <CardHeader className="pb-3">
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-base">Evidence Items</CardTitle>
                    <Button size="sm" onClick={() => navigate({ page: 'app', section: 'evidence-builder' })}>
                      <Package className="h-4 w-4 mr-1.5" /> Build Package
                    </Button>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {relatedEvidence.length > 0 ? relatedEvidence.map(ev => (
                    <div key={ev.id} className="p-3 border rounded-lg">
                      <div className="flex items-center gap-2 mb-1">
                        <Badge variant="secondary" className="text-[10px]">{ev.type}</Badge>
                        <span className="font-medium text-sm">{ev.title}</span>
                        <span className="ml-auto text-xs font-bold" style={{ color: scoreColor(ev.quality) }}>{ev.quality}</span>
                      </div>
                      <p className="text-xs text-muted-foreground">{ev.source} · {ev.year}</p>
                      <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{ev.summary}</p>
                    </div>
                  )) : <p className="text-sm text-muted-foreground">No evidence items found</p>}
                </CardContent>
              </Card>
            </div>
            <Card>
              <CardHeader className="pb-3"><CardTitle className="text-base">Gap Analysis</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                {['clinical', 'preclinical', 'computational', 'literature', 'patent'].map(type => {
                  const has = relatedEvidence.some(e => e.type === type);
                  return (
                    <div key={type} className="flex items-center gap-2">
                      {has ? <CheckCircle2 className="h-4 w-4" style={{ color: ACCENT_GREEN }} /> : <XCircle className="h-4 w-4 text-slate-300" />}
                      <span className={`text-sm ${has ? 'text-foreground' : 'text-muted-foreground'}`}>{type.charAt(0).toUpperCase() + type.slice(1)} Evidence</span>
                    </div>
                  );
                })}
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// SUB-COMPONENTS FOR CANDIDATE DETAIL
// ═══════════════════════════════════════════

function PathwayDiagram({ candidate, disease }: { candidate: DrugCandidate; disease: Disease }) {
  const relatedNodes = graphNodes.filter(n =>
    candidate.targets.includes(n.label) ||
    n.label === candidate.drugName ||
    n.label === disease.name ||
    candidate.pathways.some(p => n.label.includes(p.split(' ')[0]))
  );
  const relatedEdges = graphEdges.filter(e => {
    const srcNode = graphNodes.find(n => n.id === e.source);
    const tgtNode = graphNodes.find(n => n.id === e.target);
    return relatedNodes.some(n => n.id === e.source || n.id === e.target);
  });

  const nodeColors: Record<string, string> = { drug: PRIMARY, disease: ACCENT_RED, gene: '#3B82F6', protein: ACCENT_GREEN, pathway: ACCENT_ORANGE };
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <div className="relative">
      <svg width="100%" height="380" viewBox="0 0 800 380" className="bg-white rounded-lg border">
        <defs>
          <marker id="arrowG" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill={ACCENT_GREEN} /></marker>
          <marker id="arrowR" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill={ACCENT_RED} /></marker>
          <marker id="arrowP" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill={PRIMARY} /></marker>
        </defs>
        {/* Layout nodes in pathway style */}
        {(() => {
          const drugNode = { x: 80, y: 190, label: candidate.drugName, type: 'drug' };
          const targetNodes = candidate.targets.map((t, i) => ({ x: 260, y: 100 + i * 90, label: t, type: 'gene' }));
          const pathwayNodes = candidate.pathways.map((p, i) => ({ x: 480, y: 120 + i * 100, label: p, type: 'pathway' }));
          const diseaseNode = { x: 700, y: 190, label: disease.name, type: 'disease' };
          const allNodes = [drugNode, ...targetNodes, ...pathwayNodes, diseaseNode];
          return (
            <>
              {/* Edges: Drug → Targets */}
              {targetNodes.map((t, i) => (
                <line key={`dt${i}`} x1={drugNode.x + 40} y1={drugNode.y} x2={t.x - 30} y2={t.y}
                  stroke={PRIMARY} strokeWidth={1.5} markerEnd="url(#arrowP)" opacity={0.6} />
              ))}
              {/* Edges: Targets → Pathways */}
              {targetNodes.map((t, ti) =>
                pathwayNodes.map((p, pi) => (
                  <line key={`tp${ti}-${pi}`} x1={t.x + 30} y1={t.y} x2={p.x - 50} y2={p.y}
                    stroke={ACCENT_GREEN} strokeWidth={1} markerEnd="url(#arrowG)" opacity={0.4} />
                ))
              )}
              {/* Edges: Pathways → Disease */}
              {pathwayNodes.map((p, i) => (
                <line key={`pd${i}`} x1={p.x + 50} y1={p.y} x2={diseaseNode.x - 50} y2={diseaseNode.y}
                  stroke={ACCENT_RED} strokeWidth={1.5} markerEnd="url(#arrowR)" opacity={0.6} />
              ))}
              {/* Nodes */}
              {allNodes.map((n, i) => {
                const color = nodeColors[n.type] || PRIMARY;
                const isSel = selected === n.label;
                return (
                  <g key={i} className="cursor-pointer" onClick={() => setSelected(selected === n.label ? null : n.label)}>
                    {n.type === 'drug' ? (
                      <rect x={n.x - 40} y={n.y - 15} width={80} height={30} rx={6} fill={`${color}15`} stroke={color} strokeWidth={isSel ? 2.5 : 1.5} />
                    ) : n.type === 'disease' ? (
                      <rect x={n.x - 50} y={n.y - 15} width={100} height={30} rx={6} fill={`${color}15`} stroke={color} strokeWidth={isSel ? 2.5 : 1.5} />
                    ) : (
                      <circle cx={n.x} cy={n.y} r={22} fill={`${color}15`} stroke={color} strokeWidth={isSel ? 2.5 : 1.5} />
                    )}
                    <text x={n.x} y={n.y + 4} textAnchor="middle" className="text-[10px] fill-foreground font-medium pointer-events-none">{n.label}</text>
                  </g>
                );
              })}
            </>
          );
        })()}
      </svg>
      {/* Legend */}
      <div className="flex items-center gap-3 mt-2 justify-center">
        {Object.entries(nodeColors).map(([type, color]) => (
          <div key={type} className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: color }} /><span className="text-xs text-muted-foreground capitalize">{type}</span></div>
        ))}
      </div>
      {selected && (
        <div className="mt-3 p-3 bg-muted/50 rounded-lg border">
          <span className="font-semibold text-sm">{selected}</span>
          <p className="text-xs text-muted-foreground mt-0.5">Click to explore relationships in the Knowledge Graph</p>
        </div>
      )}
    </div>
  );
}

function ADMETRadarChart({ data }: { data: ADMETProfile }) {
  const chartData = [
    { subject: 'Absorption', value: data.absorption },
    { subject: 'Distribution', value: data.distribution },
    { subject: 'Metabolism', value: data.metabolism },
    { subject: 'Excretion', value: data.excretion },
    { subject: 'Toxicity', value: data.toxicity },
  ];
  return (
    <ResponsiveContainer width="100%" height={280}>
      <RadarChart data={chartData}>
        <PolarGrid stroke="#E2E1EA" />
        <PolarAngleAxis dataKey="subject" tick={{ fontSize: 11, fill: '#64748B' }} />
        <PolarRadiusAxis angle={30} domain={[0, 100]} tick={{ fontSize: 9 }} />
        <Radar name="ADMET" dataKey="value" stroke={PRIMARY} fill={PRIMARY} fillOpacity={0.2} strokeWidth={2} />
      </RadarChart>
    </ResponsiveContainer>
  );
}

function PhaseDistributionChart({ trials }: { trials: ClinicalTrial[] }) {
  const phaseCounts = trials.reduce<Record<string, number>>((acc, t) => { acc[t.phase] = (acc[t.phase] || 0) + 1; return acc; }, {});
  const data = Object.entries(phaseCounts).map(([name, value]) => ({ name, value }));
  const COLORS = [PRIMARY, ACCENT_GREEN, ACCENT_ORANGE, '#8B5CF6', ACCENT_RED];
  return data.length > 0 ? (
    <ResponsiveContainer width="100%" height={200}>
      <RechartsPie>
        <Pie data={data} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70} label={({ name, value }) => `${name}: ${value}`}>
          {data.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
        </Pie>
        <RechartsTooltip />
      </RechartsPie>
    </ResponsiveContainer>
  ) : <p className="text-sm text-muted-foreground text-center py-8">No trial data</p>;
}

function PatentTimeline({ patents }: { patents: Patent[] }) {
  if (patents.length === 0) return <p className="text-sm text-muted-foreground">No patent data</p>;
  return (
    <div className="space-y-3">
      {patents.map(p => (
        <div key={p.id} className="flex items-center gap-2">
          <div className="w-3 h-3 rounded-full" style={{ backgroundColor: p.status === 'active' ? ACCENT_GREEN : p.status === 'pending' ? ACCENT_ORANGE : '#94A3B8' }} />
          <div className="flex-1 min-w-0">
            <p className="text-xs font-medium truncate">{p.patentNumber}</p>
            <p className="text-[10px] text-muted-foreground">{p.filingDate.slice(0,4)} → {p.expirationDate.slice(0,4)}</p>
          </div>
          <Badge variant={p.status === 'active' ? 'default' : 'secondary'} className="text-[10px]">{p.status}</Badge>
        </div>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════
// 4. KNOWLEDGE GRAPH SCREEN
// ═══════════════════════════════════════════

function KnowledgeGraphScreen() {
  const { navigate } = useDrugOSNav();
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [nodeFilters, setNodeFilters] = useState<Record<string, boolean>>({ drug: true, disease: true, gene: true, protein: true, pathway: true });
  const [evidenceThreshold, setEvidenceThreshold] = useState(0.3);
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }>>(() => new Map(graphNodes.map(n => [n.id, { x: n.x * 1.2, y: n.y * 0.7 + 50 }])));

  const filteredNodes = graphNodes.filter(n => nodeFilters[n.type]);
  const filteredEdges = graphEdges.filter(e => {
    const src = graphNodes.find(n => n.id === e.source);
    const tgt = graphNodes.find(n => n.id === e.target);
    return e.evidence >= evidenceThreshold && src && tgt && nodeFilters[src.type] && nodeFilters[tgt.type];
  });

  const searchedNodes = searchQuery.length >= 2
    ? filteredNodes.filter(n => n.label.toLowerCase().includes(searchQuery.toLowerCase()))
    : filteredNodes;

  const nodeColors: Record<string, string> = { drug: PRIMARY, disease: ACCENT_RED, gene: '#3B82F6', protein: ACCENT_GREEN, pathway: ACCENT_ORANGE };
  const nodeSizes: Record<string, number> = { drug: 22, disease: 26, gene: 18, protein: 20, pathway: 18 };

  const connectedToSelected = useMemo(() => {
    if (!selectedNode) return new Set<string>();
    const s = new Set<string>();
    s.add(selectedNode);
    filteredEdges.forEach(e => {
      if (e.source === selectedNode) s.add(e.target);
      if (e.target === selectedNode) s.add(e.source);
    });
    return s;
  }, [selectedNode, filteredEdges]);

  return (
    <FadeIn>
      <PageHeader title="Knowledge Graph Explorer" description="Explore relationships between drugs, diseases, genes, proteins, and pathways" />

      <div className="flex flex-col lg:flex-row gap-4">
        {/* Sidebar */}
        <div className="w-full lg:w-64 space-y-4 shrink-0">
          <Card>
            <CardContent className="p-4">
              <Input value={searchQuery} onChange={e => setSearchQuery(e.target.value)} placeholder="Search entities..." className="mb-3" />
              <div className="space-y-2">
                <p className="text-xs font-semibold text-muted-foreground">Node Types</p>
                {Object.entries(nodeFilters).map(([type, checked]) => (
                  <label key={type} className="flex items-center gap-2 cursor-pointer">
                    <Checkbox checked={checked} onCheckedChange={v => setNodeFilters(p => ({ ...p, [type]: !!v }))} />
                    <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: nodeColors[type] }} />
                    <span className="text-sm capitalize">{type}</span>
                    <span className="ml-auto text-xs text-muted-foreground">{graphNodes.filter(n => n.type === type).length}</span>
                  </label>
                ))}
              </div>
              <Separator className="my-3" />
              <div>
                <p className="text-xs font-semibold text-muted-foreground mb-2">Evidence Threshold: {evidenceThreshold.toFixed(1)}</p>
                <Slider value={[evidenceThreshold]} onValueChange={v => setEvidenceThreshold(v[0])} min={0} max={1} step={0.1} />
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <p className="text-xs font-semibold text-muted-foreground mb-2">Statistics</p>
              <div className="space-y-1 text-sm">
                <div className="flex justify-between"><span className="text-muted-foreground">Nodes</span><span className="font-medium">{searchedNodes.length}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Edges</span><span className="font-medium">{filteredEdges.length}</span></div>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <p className="text-xs font-semibold text-muted-foreground mb-2">Quick Start</p>
              <div className="space-y-1.5">
                <button className="text-xs text-primary hover:underline block w-full text-left">Find drugs targeting BRCA1</button>
                <button className="text-xs text-primary hover:underline block w-full text-left">Show pathways in Alzheimer's</button>
                <button className="text-xs text-primary hover:underline block w-full text-left">Memantine mechanism of action</button>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Graph Area */}
        <Card className="flex-1">
          <CardContent className="p-0 relative">
            <div className="absolute top-3 right-3 z-10 flex items-center gap-1">
              <Button variant="outline" size="sm" onClick={() => setZoom(z => Math.min(z + 0.2, 3))} className="h-7 w-7 p-0"><ZoomIn className="h-3.5 w-3.5" /></Button>
              <span className="text-xs text-muted-foreground w-10 text-center">{Math.round(zoom * 100)}%</span>
              <Button variant="outline" size="sm" onClick={() => setZoom(z => Math.max(z - 0.2, 0.3))} className="h-7 w-7 p-0"><ZoomOut className="h-3.5 w-3.5" /></Button>
              <Button variant="outline" size="sm" onClick={() => { setZoom(1); setSelectedNode(null); }} className="h-7 w-7 p-0"><RotateCcw className="h-3.5 w-3.5" /></Button>
            </div>
            <svg width="100%" height={500} viewBox="0 0 800 500" className="rounded-lg">
              <g transform={`translate(400,250) scale(${zoom}) translate(-400,-250)`}>
                {filteredEdges.map((e, i) => {
                  const src = positions.get(e.source);
                  const tgt = positions.get(e.target);
                  if (!src || !tgt) return null;
                  const isHighlighted = !selectedNode || connectedToSelected.has(e.source) && connectedToSelected.has(e.target);
                  return (
                    <g key={i} opacity={isHighlighted ? 0.6 : 0.1}>
                      <line x1={src.x} y1={src.y} x2={tgt.x} y2={tgt.y}
                        stroke={e.evidence > 0.9 ? ACCENT_GREEN : e.evidence > 0.7 ? PRIMARY : ACCENT_ORANGE}
                        strokeWidth={e.evidence > 0.9 ? 2 : 1}
                        strokeDasharray={e.evidence < 0.7 ? '4 3' : undefined}
                      />
                      <text x={(src.x + tgt.x) / 2} y={(src.y + tgt.y) / 2 - 5} textAnchor="middle" className="text-[8px] fill-muted-foreground pointer-events-none">{e.relation}</text>
                    </g>
                  );
                })}
                {searchedNodes.map(n => {
                  const pos = positions.get(n.id);
                  if (!pos) return null;
                  const isSel = selectedNode === n.id;
                  const isConn = connectedToSelected.has(n.id);
                  const isActive = !selectedNode || isConn;
                  const r = nodeSizes[n.type] || 20;
                  const color = nodeColors[n.type];
                  return (
                    <g key={n.id} className="cursor-pointer" onClick={() => setSelectedNode(selectedNode === n.id ? null : n.id)} opacity={isActive ? 1 : 0.2}>
                      {isSel && <circle cx={pos.x} cy={pos.y} r={r + 6} fill="none" stroke={color} strokeWidth={2} strokeDasharray="4 2" opacity={0.5} />}
                      <circle cx={pos.x} cy={pos.y} r={r} fill={`${color}15`} stroke={color} strokeWidth={isSel ? 2.5 : 1.5} />
                      <text x={pos.x} y={pos.y + r + 13} textAnchor="middle" className="text-[9px] fill-foreground font-medium pointer-events-none">{n.label}</text>
                    </g>
                  );
                })}
              </g>
            </svg>
            {/* Selected node info */}
            {selectedNode && (() => {
              const node = graphNodes.find(n => n.id === selectedNode);
              if (!node) return null;
              const nodeEdges = filteredEdges.filter(e => e.source === selectedNode || e.target === selectedNode);
              return (
                <div className="absolute bottom-3 left-3 bg-background/90 backdrop-blur-sm border rounded-lg p-3 max-w-[240px]">
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-semibold text-sm">{node.label}</span>
                    <Button variant="ghost" size="sm" className="h-5 w-5 p-0" onClick={() => setSelectedNode(null)}>×</Button>
                  </div>
                  <Badge variant="secondary" className="text-[10px]" style={{ color: nodeColors[node.type] }}>{node.type}</Badge>
                  <p className="text-xs text-muted-foreground mt-1">{nodeEdges.length} connections</p>
                  {node.type === 'drug' && (
                    <Button variant="link" size="sm" className="h-6 p-0 text-xs mt-1" onClick={() => {
                      const cand = drugCandidates.find(c => c.drugName === node.label);
                      if (cand) navigate({ page: 'app', section: 'candidate', id: cand.id });
                    }}>View candidate detail →</Button>
                  )}
                </div>
              );
            })()}
          </CardContent>
        </Card>
      </div>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// 5. CLINICAL TRIALS SCREEN
// ═══════════════════════════════════════════

function ClinicalTrialsScreen() {
  const [search, setSearch] = useState('');
  const [phaseFilter, setPhaseFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [selectedTrial, setSelectedTrial] = useState<ClinicalTrial | null>(null);
  const [liveTrials, setLiveTrials] = useState<APIClinicalTrial[]>([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveCondition, setLiveCondition] = useState<string>('');

  // Live ClinicalTrials.gov search via /api/clinical-trials/search.
  useEffect(() => {
    if (search.trim().length < 3) {
      setLiveTrials([]);
      setLiveError(null);
      setLiveCondition('');
      return;
    }
    let cancelled = false;
    setLiveLoading(true);
    setLiveError(null);
    const t = setTimeout(async () => {
      try {
        const res = await api.clinicalTrials.search({ q: search.trim(), limit: 25 });
        if (!cancelled) {
          setLiveTrials(res.trials || []);
          setLiveCondition(search.trim());
        }
      } catch (e) {
        if (!cancelled) setLiveError(errorMessage(e));
      } finally {
        if (!cancelled) setLiveLoading(false);
      }
    }, 500);
    return () => { cancelled = true; clearTimeout(t); };
  }, [search]);

  const filtered = useMemo(() => {
    return clinicalTrials.filter(t => {
      const matchSearch = !search || t.title.toLowerCase().includes(search.toLowerCase()) || t.nctId.toLowerCase().includes(search.toLowerCase()) || t.drugName.toLowerCase().includes(search.toLowerCase());
      const matchPhase = phaseFilter === 'all' || t.phase === phaseFilter;
      const matchStatus = statusFilter === 'all' || t.status === statusFilter;
      return matchSearch && matchPhase && matchStatus;
    });
  }, [search, phaseFilter, statusFilter]);

  const phases = [...new Set(clinicalTrials.map(t => t.phase))];
  const statuses = [...new Set(clinicalTrials.map(t => t.status))];

  return (
    <FadeIn>
      <PageHeader title="Clinical Trial Search" description="Live search across ClinicalTrials.gov — real registered trials" />

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search live ClinicalTrials.gov — e.g. 'breast cancer', 'metformin', 'NCT00001'" className="pl-9" />
        </div>
        {liveLoading && <span className="text-xs text-muted-foreground flex items-center gap-1.5"><RefreshCw className="w-3.5 h-3.5 animate-spin" /> Querying ClinicalTrials.gov…</span>}
      </div>

      {liveError && (
        <div className="bg-amber-50 border border-amber-200 text-amber-800 text-sm rounded-md px-3 py-2 mb-4">
          Live search failed: {liveError}. Showing sample data below.
        </div>
      )}

      {liveCondition && !liveLoading && (
        <div className="text-xs text-muted-foreground mb-3">
          Showing {liveTrials.length} live result{liveTrials.length === 1 ? '' : 's'} from ClinicalTrials.gov for &ldquo;{liveCondition}&rdquo;
        </div>
      )}

      {/* Live ClinicalTrials.gov results take priority when available */}
      {liveTrials.length > 0 ? (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader><TableRow className="bg-muted/50"><TableHead>NCT ID</TableHead><TableHead>Title</TableHead><TableHead>Phase</TableHead><TableHead>Status</TableHead><TableHead>Enrollment</TableHead><TableHead>Conditions</TableHead></TableRow></TableHeader>
              <TableBody>
                {liveTrials.map(t => (
                  <TableRow
                    key={t.nctId}
                    className="cursor-pointer hover:bg-muted/30"
                    onClick={() => setSelectedTrial(selectedTrial?.nctId === t.nctId ? null : {
                      id: t.nctId,
                      nctId: t.nctId,
                      title: t.title,
                      phase: t.phase || 'N/A',
                      status: t.status,
                      enrollment: t.enrollment || 0,
                      startDate: t.startDate || '',
                      completionDate: t.completionDate || '',
                      drugName: (t.interventions || []).join(', ') || 'N/A',
                      disease: (t.conditions || []).join(', ') || 'N/A',
                      outcome: t.studyType || 'See registry',
                    } as ClinicalTrial)}
                  >
                    <TableCell><span className="font-mono text-xs text-primary">{t.nctId}</span></TableCell>
                    <TableCell className="max-w-[300px]"><span className="text-sm line-clamp-2">{t.title}</span></TableCell>
                    <TableCell><Badge variant="secondary" className="text-xs">{t.phase || 'N/A'}</Badge></TableCell>
                    <TableCell><Badge className="text-xs">{t.status}</Badge></TableCell>
                    <TableCell className="text-sm">{t.enrollment ?? '—'}</TableCell>
                    <TableCell className="text-xs text-muted-foreground max-w-[200px] truncate">{(t.conditions || []).join(', ')}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader><TableRow className="bg-muted/50"><TableHead>NCT ID</TableHead><TableHead>Title</TableHead><TableHead>Phase</TableHead><TableHead>Status</TableHead><TableHead>Enrollment</TableHead><TableHead>Dates</TableHead></TableRow></TableHeader>
              <TableBody>
                {filtered.map(t => (
                  <TableRow key={t.id} className="cursor-pointer hover:bg-muted/30" onClick={() => setSelectedTrial(selectedTrial?.id === t.id ? null : t)}>
                    <TableCell><span className="font-mono text-xs text-primary">{t.nctId}</span></TableCell>
                    <TableCell className="max-w-[300px]"><span className="text-sm line-clamp-2">{t.title}</span></TableCell>
                    <TableCell><Badge variant="secondary" className="text-xs">{t.phase}</Badge></TableCell>
                    <TableCell><Badge className="text-xs">{t.status}</Badge></TableCell>
                    <TableCell className="text-sm">{t.enrollment}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{t.startDate} → {t.completionDate}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {selectedTrial && (
        <Card className="mt-4">
          <CardHeader className="pb-3"><CardTitle className="text-base">{selectedTrial.title}</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <div><span className="text-muted-foreground">NCT ID:</span> <span className="font-mono">{selectedTrial.nctId}</span></div>
              <div><span className="text-muted-foreground">Phase:</span> <Badge variant="secondary">{selectedTrial.phase}</Badge></div>
              <div><span className="text-muted-foreground">Status:</span> <Badge>{selectedTrial.status}</Badge></div>
              <div><span className="text-muted-foreground">Enrollment:</span> {selectedTrial.enrollment}</div>
            </div>
            <div><span className="text-muted-foreground">Drug:</span> {selectedTrial.drugName} · <span className="text-muted-foreground">Disease:</span> {selectedTrial.disease}</div>
            <div><span className="text-muted-foreground">Outcome:</span> {selectedTrial.outcome}</div>
            <a
              href={`https://clinicaltrials.gov/study/${selectedTrial.nctId}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-primary text-xs hover:underline mt-2"
            >
              View on ClinicalTrials.gov <ExternalLink className="w-3 h-3" />
            </a>
          </CardContent>
        </Card>
      )}
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// 6. SAFETY PROFILE SCREEN
// ═══════════════════════════════════════════

function SafetyProfileScreen() {
  const [selectedDrug, setSelectedDrug] = useState<string>(drugCandidates[0].drugName);
  const candidate = drugCandidates.find(c => c.drugName === selectedDrug) || drugCandidates[0];
  const admet = admetProfiles.find(a => a.drugName === selectedDrug);
  const offTargets = offTargetPredictions.filter(o => o.drugName === selectedDrug);
  const interactions = drugInteractions.filter(d => d.drug1 === selectedDrug);
  const [ddiQuery, setDdiQuery] = useState('');
  const [fdaQuery, setFdaQuery] = useState<string>('');
  const [fdaData, setFdaData] = useState<OpenFdaAdverseEvent | null>(null);
  const [fdaLoading, setFdaLoading] = useState(false);
  const [fdaError, setFdaError] = useState<string | null>(null);
  const uniqueDrugNames = [...new Set(drugCandidates.map(c => c.drugName))];

  // Whenever the selected drug changes, prime the FDA query box with it.
  useEffect(() => {
    setFdaQuery(selectedDrug);
    setFdaData(null);
    setFdaError(null);
  }, [selectedDrug]);

  const fetchFda = useCallback(async (drug: string) => {
    if (!drug.trim()) return;
    setFdaLoading(true);
    setFdaError(null);
    try {
      const data = await api.safety.get(drug.trim());
      setFdaData(data);
    } catch (e) {
      setFdaError(errorMessage(e));
      setFdaData(null);
    } finally {
      setFdaLoading(false);
    }
  }, []);

  // Auto-fetch when drug changes (debounced).
  useEffect(() => {
    if (!fdaQuery.trim()) return;
    const t = setTimeout(() => fetchFda(fdaQuery), 500);
    return () => clearTimeout(t);
  }, [fdaQuery, fetchFda]);

  const ddiResults = useMemo(() => {
    if (!ddiQuery.trim()) return [];
    return drugInteractions.filter(d =>
      (d.drug1.toLowerCase().includes(ddiQuery.toLowerCase()) || d.drug2.toLowerCase().includes(ddiQuery.toLowerCase())) &&
      (d.drug1 === selectedDrug || d.drug2 === selectedDrug)
    );
  }, [ddiQuery, selectedDrug]);

  return (
    <FadeIn>
      <PageHeader title="Safety Profile Dashboard" description="Comprehensive safety analysis with live FDA adverse-event data" />

      <div className="mb-4">
        <Select value={selectedDrug} onValueChange={setSelectedDrug}>
          <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
          <SelectContent>{uniqueDrugNames.map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
        </Select>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
        <StatCard icon={ShieldCheck} value={candidate.safetyScore} label="Safety Score" color={ACCENT_GREEN} />
        <StatCard icon={AlertTriangle} value={offTargets.length} label="Off-Target Predictions" color={ACCENT_ORANGE} />
        <StatCard icon={AlertCircle} value={interactions.length} label="Drug Interactions" color={ACCENT_RED} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">Safety Tier</CardTitle>
              <SafetyBadge tier={candidate.safetyTier} />
            </div>
          </CardHeader>
          <CardContent>
            {admet && <ADMETRadarChart data={admet} />}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">Off-Target Interaction Profile</CardTitle></CardHeader>
          <CardContent>
            {offTargets.length > 0 ? (
              <div className="space-y-2">
                {offTargets.map((o, i) => (
                  <div key={i} className="flex items-center justify-between p-2.5 border rounded-lg">
                    <div>
                      <span className="text-sm font-medium">{o.target}</span>
                      <span className="text-xs text-muted-foreground ml-2">({o.organSystem})</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs">{Math.round(o.probability * 100)}%</span>
                      <Badge variant={o.severity === 'high' ? 'destructive' : o.severity === 'medium' ? 'secondary' : 'outline'} className="text-xs">{o.severity}</Badge>
                    </div>
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-muted-foreground">No off-target predictions</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">Drug-Drug Interaction Checker</CardTitle></CardHeader>
          <CardContent>
            <div className="relative mb-3">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input value={ddiQuery} onChange={e => setDdiQuery(e.target.value)} placeholder="Enter medication name to check..." className="pl-9" />
            </div>
            {ddiResults.length > 0 ? ddiResults.map((r, i) => (
              <div key={i} className="p-2.5 border rounded-lg mb-2">
                <div className="flex items-center gap-2">
                  <Badge variant={r.severity === 'contraindicated' ? 'destructive' : r.severity === 'major' ? 'secondary' : 'outline'} className="text-xs">{r.severity}</Badge>
                  <span className="text-sm">{r.drug1} ↔ {r.drug2}</span>
                </div>
                <p className="text-xs text-muted-foreground mt-1">{r.description}</p>
              </div>
            )) : ddiQuery && <p className="text-sm text-muted-foreground">No interactions found</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center justify-between">
              Adverse Event Signals
              <Badge variant="outline" className="text-xs">live · openFDA FAERS</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                value={fdaQuery}
                onChange={e => setFdaQuery(e.target.value)}
                placeholder="Enter drug name (e.g. metformin, aspirin)..."
                className="pl-9"
              />
            </div>

            {fdaLoading && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <RefreshCw className="w-3.5 h-3.5 animate-spin" /> Querying FDA Adverse Event Reporting System…
              </div>
            )}

            {fdaError && !fdaLoading && (
              <div className="bg-amber-50 border border-amber-200 text-amber-800 text-xs rounded-md px-3 py-2">
                openFDA lookup failed: {fdaError}
              </div>
            )}

            {fdaData && !fdaLoading && (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <div className="p-3 border rounded-lg">
                    <div className="text-xs text-muted-foreground">Total reports</div>
                    <div className="text-2xl font-bold">{fdaData.totalReports.toLocaleString()}</div>
                  </div>
                  <div className="p-3 border rounded-lg">
                    <div className="text-xs text-muted-foreground">Serious reports</div>
                    <div className="text-2xl font-bold text-amber-700">{fdaData.seriousReports.toLocaleString()}</div>
                  </div>
                </div>

                {fdaData.topReactions.length > 0 && (
                  <div className="space-y-1.5">
                    <div className="text-xs font-semibold text-muted-foreground">Top reported reactions</div>
                    {fdaData.topReactions.slice(0, 8).map((r, i) => {
                      const max = fdaData.topReactions[0]?.count || 1;
                      const pct = Math.round((r.count / max) * 100);
                      return (
                        <div key={i} className="flex items-center justify-between">
                          <span className="text-sm">{r.term}</span>
                          <div className="flex items-center gap-2">
                            <div className="w-20 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                              <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: ACCENT_ORANGE }} />
                            </div>
                            <span className="text-xs text-muted-foreground tabular-nums">{r.count}</span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}

                {fdaData.safetyDisclaimer && (
                  <div className="mt-3 p-2.5 bg-amber-50 border border-amber-300 rounded-lg">
                    <div className="flex items-start gap-2">
                      <AlertTriangle className="h-4 w-4 text-amber-700 shrink-0 mt-0.5" />
                      <div>
                        <div className="text-xs font-semibold text-amber-800">FDA FAERS Disclaimer</div>
                        <p className="text-xs text-amber-700 mt-0.5">{fdaData.safetyDisclaimer}</p>
                      </div>
                    </div>
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// 7. IP PATENTS SCREEN
// ═══════════════════════════════════════════

function IPPatentsScreen() {
  const [selectedDrug, setSelectedDrug] = useState<string>(drugCandidates[0].drugName);
  const uniqueDrugNames = [...new Set(drugCandidates.map(c => c.drugName))];
  const relatedPatents = patents.filter(p => p.drugName === selectedDrug);
  const candidate = drugCandidates.find(c => c.drugName === selectedDrug);

  return (
    <FadeIn>
      <PageHeader title="IP & Patent Status" description="Track intellectual property and patent status for candidates" />

      <div className="mb-4">
        <Select value={selectedDrug} onValueChange={setSelectedDrug}>
          <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
          <SelectContent>{uniqueDrugNames.map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
        </Select>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4 mb-6">
        <StatCard icon={Scale} value={patents.filter(p => p.status === 'active').length} label="Active Patents" color={ACCENT_GREEN} />
        <StatCard icon={Clock} value={patents.filter(p => p.status === 'pending').length} label="Pending" color={ACCENT_ORANGE} />
        <StatCard icon={FileText} value={patents.filter(p => p.status === 'expired').length} label="Expired" />
        <StatCard icon={AlertCircle} value={patents.filter(p => p.status === 'abandoned').length} label="Abandoned" color={ACCENT_RED} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Patent Search Results</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              {relatedPatents.length > 0 ? relatedPatents.map(p => (
                <div key={p.id} className="p-4 border rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-medium text-sm">{p.title}</span>
                    <Badge variant={p.status === 'active' ? 'default' : p.status === 'expired' ? 'secondary' : p.status === 'pending' ? 'outline' : 'destructive'}>{p.status}</Badge>
                  </div>
                  <div className="text-xs text-muted-foreground space-y-0.5">
                    <p>{p.patentNumber} · {p.jurisdiction} · {p.claims} claims</p>
                    <p>Assignee: {p.assignee}</p>
                    <p>Filed: {p.filingDate} · Expires: {p.expirationDate}</p>
                  </div>
                </div>
              )) : <p className="text-sm text-muted-foreground">No patents found for {selectedDrug}</p>}
            </CardContent>
          </Card>
        </div>
        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Freedom to Operate</CardTitle></CardHeader>
            <CardContent>
              <div className="text-center">
                <div className="text-3xl font-bold" style={{ color: candidate?.ipStatus === 'Off-Patent' || candidate?.ipStatus === 'Patent Expired' ? ACCENT_GREEN : ACCENT_ORANGE }}>
                  {candidate?.ipStatus === 'Off-Patent' || candidate?.ipStatus === 'Patent Expired' ? 'Clear' : candidate?.ipStatus === 'Novel Use Patentable' ? 'Partial' : 'Restricted'}
                </div>
                <p className="text-sm text-muted-foreground mt-1">{candidate?.ipStatus}</p>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">IP Risk Score</CardTitle></CardHeader>
            <CardContent>
              <div className="text-center">
                <div className="text-3xl font-bold" style={{ color: scoreColor(candidate?.compositeScore || 50) }}>{Math.round(60 + Math.random() * 35)}</div>
                <p className="text-sm text-muted-foreground mt-1">out of 100</p>
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Patent Timeline</CardTitle></CardHeader>
            <CardContent><PatentTimeline patents={relatedPatents} /></CardContent>
          </Card>
        </div>
      </div>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// 8. EVIDENCE BUILDER SCREEN
// ═══════════════════════════════════════════

function EvidenceBuilderScreen() {
  const [selectedDrug, setSelectedDrug] = useState<string>('Memantine');
  const [selectedDisease, setSelectedDisease] = useState<string>("Huntington's Disease");
  const [selectedEvidence, setSelectedEvidence] = useState<Set<string>>(new Set());
  const [template, setTemplate] = useState('internal');
  const uniqueDrugNames = [...new Set(drugCandidates.map(c => c.drugName))];

  // Live PubMed literature for the selected drug + disease.
  const [liveArticles, setLiveArticles] = useState<PubMedArticle[]>([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);

  // Live evidence packages created via /api/evidence-package (real backend).
  const [builtPackage, setBuiltPackage] = useState<EvidencePackage | null>(null);
  const [building, setBuilding] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);

  useEffect(() => {
    const q = `${selectedDrug} ${selectedDisease}`.trim();
    if (!q) return;
    let cancelled = false;
    setLiveLoading(true);
    setLiveError(null);
    const t = setTimeout(async () => {
      try {
        const res = await api.literature.search(q, 15);
        if (!cancelled) setLiveArticles(res.articles || []);
      } catch (e) {
        if (!cancelled) setLiveError(errorMessage(e));
      } finally {
        if (!cancelled) setLiveLoading(false);
      }
    }, 400);
    return () => { cancelled = true; clearTimeout(t); };
  }, [selectedDrug, selectedDisease]);

  const availableEvidence = evidenceItems.filter(e => e.drugName === selectedDrug);
  const diseaseEvidence = evidenceItems.filter(e => e.disease === selectedDisease);

  const toggleEvidence = (id: string) => {
    setSelectedEvidence(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const handleBuildPackage = async () => {
    setBuilding(true);
    setBuildError(null);
    try {
      const pkg = await api.evidence.create({
        drugName: selectedDrug,
        diseaseName: selectedDisease,
      });
      setBuiltPackage(pkg);
    } catch (e) {
      setBuildError(errorMessage(e));
    } finally {
      setBuilding(false);
    }
  };

  const templates = [
    { id: 'internal', name: 'Internal Review' },
    { id: 'pre-ind', name: 'Pre-IND' },
    { id: 'investor', name: 'Investor' },
    { id: 'partnership', name: 'Partnership' },
    { id: 'publication', name: 'Publication' },
    { id: 'grant', name: 'Grant' },
  ];

  return (
    <FadeIn>
      <PageHeader title="Evidence Package Builder" description="Build comprehensive evidence packages — live PubMed literature aggregated by the backend" />

      <div className="flex flex-wrap items-center gap-3 mb-6">
        <Select value={selectedDrug} onValueChange={setSelectedDrug}>
          <SelectTrigger className="w-48"><SelectValue placeholder="Select Drug" /></SelectTrigger>
          <SelectContent>{uniqueDrugNames.map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
        </Select>
        <Select value={selectedDisease} onValueChange={setSelectedDisease}>
          <SelectTrigger className="w-56"><SelectValue placeholder="Select Disease" /></SelectTrigger>
          <SelectContent>{diseases.map(d => <SelectItem key={d.id} value={d.name}>{d.name}</SelectItem>)}</SelectContent>
        </Select>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Available Evidence — real PubMed articles + local mock fallback */}
        <Card className="lg:col-span-2">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base flex items-center gap-2">
                Live PubMed Literature
                <Badge variant="outline" className="text-[10px]">{liveArticles.length} articles</Badge>
              </CardTitle>
              <Badge variant="secondary">{selectedEvidence.size} selected</Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-2 max-h-96 overflow-y-auto">
            {liveLoading && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground p-3">
                <RefreshCw className="w-3.5 h-3.5 animate-spin" /> Querying PubMed…
              </div>
            )}
            {liveError && !liveLoading && (
              <div className="bg-amber-50 border border-amber-200 text-amber-800 text-xs rounded-md px-3 py-2">
                PubMed lookup failed: {liveError}
              </div>
            )}
            {!liveLoading && liveArticles.map((a, i) => {
              const id = `pmid-${a.pmid}`;
              return (
                <div
                  key={id}
                  className={`p-3 border rounded-lg cursor-pointer transition-colors ${selectedEvidence.has(id) ? 'border-primary bg-primary/5' : 'hover:bg-accent'}`}
                  onClick={() => toggleEvidence(id)}
                >
                  <div className="flex items-center gap-2">
                    {selectedEvidence.has(id) ? <CheckSquare className="h-4 w-4 text-primary" /> : <Square className="h-4 w-4 text-muted-foreground" />}
                    <Badge variant="secondary" className="text-[10px]">PMID {a.pmid}</Badge>
                    <span className="text-sm font-medium flex-1 line-clamp-1">{a.title}</span>
                    {a.journal && <span className="text-xs text-muted-foreground italic truncate max-w-[200px]">{a.journal}</span>}
                  </div>
                  {a.authors && a.authors.length > 0 && (
                    <p className="text-xs text-muted-foreground mt-1 ml-6">
                      {a.authors.slice(0, 3).join(', ')}{a.authors.length > 3 ? ' et al.' : ''}
                      {a.pubDate ? ` · ${a.pubDate}` : ''}
                    </p>
                  )}
                </div>
              );
            })}
            {!liveLoading && liveArticles.length === 0 && (
              <div className="space-y-2">
                <div className="text-xs text-muted-foreground mb-2">Sample evidence items:</div>
                {[...availableEvidence, ...diseaseEvidence].filter((e, i, arr) => arr.findIndex(x => x.id === e.id) === i).map(ev => (
                  <div key={ev.id} className={`p-3 border rounded-lg cursor-pointer transition-colors ${selectedEvidence.has(ev.id) ? 'border-primary bg-primary/5' : 'hover:bg-accent'}`} onClick={() => toggleEvidence(ev.id)}>
                    <div className="flex items-center gap-2">
                      {selectedEvidence.has(ev.id) ? <CheckSquare className="h-4 w-4 text-primary" /> : <Square className="h-4 w-4 text-muted-foreground" />}
                      <Badge variant="secondary" className="text-[10px]">{ev.type}</Badge>
                      <span className="text-sm font-medium flex-1">{ev.title}</span>
                      <span className="text-xs font-bold" style={{ color: scoreColor(ev.quality) }}>{ev.quality}</span>
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 ml-6">{ev.source} · {ev.year}</p>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <div className="space-y-4">
          {/* Selected Evidence Panel */}
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Selected ({selectedEvidence.size})</CardTitle></CardHeader>
            <CardContent>
              {selectedEvidence.size === 0 ? (
                <p className="text-sm text-muted-foreground">Click evidence items to add them</p>
              ) : (
                <div className="space-y-1">
                  {[...selectedEvidence].map(id => {
                    const pmidMatch = id.match(/^pmid-(\d+)$/);
                    const pmid = pmidMatch?.[1];
                    const article = pmid ? liveArticles.find(a => a.pmid === pmid) : null;
                    const ev = !pmid ? evidenceItems.find(e => e.id === id) : null;
                    const title = article?.title || ev?.title || id;
                    return (
                      <div key={id} className="flex items-center gap-2 text-xs p-1.5 bg-accent rounded">
                        <span className="flex-1 truncate">{title}</span>
                        <button onClick={() => toggleEvidence(id)} className="text-muted-foreground hover:text-foreground"><XCircle className="h-3.5 w-3.5" /></button>
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Template Selection */}
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Template</CardTitle></CardHeader>
            <CardContent className="space-y-1.5">
              {templates.map(t => (
                <button key={t.id} onClick={() => setTemplate(t.id)} className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${template === t.id ? 'bg-primary/10 text-primary font-medium' : 'hover:bg-accent'}`}>
                  {t.name}
                </button>
              ))}
            </CardContent>
          </Card>

          {/* Built package — real backend response */}
          {builtPackage && (
            <Card className="border-emerald-300 bg-emerald-50/50">
              <CardHeader className="pb-3">
                <CardTitle className="text-base text-emerald-800 flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4" /> Evidence Package Built
                </CardTitle>
              </CardHeader>
              <CardContent className="text-xs text-emerald-900 space-y-1">
                <div><strong>Drug:</strong> {builtPackage.drugName}</div>
                <div><strong>Disease:</strong> {builtPackage.diseaseName}</div>
                <div><strong>PubMed articles:</strong> {builtPackage.literatureCount}</div>
                <div><strong>Clinical trials:</strong> {builtPackage.trialsCount}</div>
                <div><strong>Adverse-event reports:</strong> {builtPackage.safetyReportsCount}</div>
                <div className="mt-2 pt-2 border-t border-emerald-200">
                  <div className="font-semibold mb-1">Markdown preview (first 300 chars):</div>
                  <pre className="whitespace-pre-wrap font-mono text-[10px] bg-white border border-emerald-200 p-2 rounded">
                    {builtPackage.markdown.slice(0, 300)}…
                  </pre>
                </div>
              </CardContent>
            </Card>
          )}

          {buildError && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-md px-3 py-2">
              Build failed: {buildError}
            </div>
          )}

          {/* Actions */}
          <div className="space-y-2">
            <Button
              variant="outline"
              className="w-full"
              onClick={handleBuildPackage}
              disabled={building}
            >
              {building ? (
                <>
                  <RefreshCw className="h-4 w-4 mr-2 animate-spin" /> Building…
                </>
              ) : (
                <>
                  <Package className="h-4 w-4 mr-2" /> Build Evidence Package
                </>
              )}
            </Button>
            <p className="text-[11px] text-muted-foreground text-center">
              The backend aggregates live PubMed literature + ClinicalTrials.gov + openFDA adverse-event data.
              No model predictions are included — only authoritative public sources.
            </p>
          </div>
        </div>
      </div>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// 9. REPORT GENERATION SCREEN
// ═══════════════════════════════════════════

function ReportGenerationScreen() {
  const [template, setTemplate] = useState('standard');
  const [selectedDisease, setSelectedDisease] = useState('D001');
  const [generating, setGenerating] = useState(false);

  const templates = [
    { id: 'standard', name: 'Standard Report', desc: 'Comprehensive analysis with all sections', icon: FileText },
    { id: 'executive', name: 'Executive Summary', desc: 'High-level overview for decision makers', icon: BarChart3 },
    { id: 'detailed', name: 'Detailed Analysis', desc: 'Full technical deep-dive', icon: BookOpen },
    { id: 'custom', name: 'Custom Report', desc: 'Configure your own sections', icon: Settings },
  ];

  const candidates = drugCandidates.filter(c => c.diseaseId === selectedDisease);

  const handleGenerate = () => {
    setGenerating(true);
    setTimeout(() => setGenerating(false), 2000);
  };

  return (
    <FadeIn>
      <PageHeader title="Report Generation" description="Generate and preview repurposing analysis reports" />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Template Gallery */}
        <div className="lg:col-span-2 space-y-4">
          <h3 className="text-sm font-semibold text-muted-foreground">Report Template</h3>
          <div className="grid grid-cols-2 gap-3">
            {templates.map(t => (
              <Card key={t.id} className={`cursor-pointer transition-all ${template === t.id ? 'border-primary ring-2 ring-primary/20' : 'hover:border-primary/30'}`} onClick={() => setTemplate(t.id)}>
                <CardContent className="p-4">
                  <t.icon className="h-6 w-6 mb-2" style={{ color: PRIMARY }} />
                  <h4 className="font-medium text-sm">{t.name}</h4>
                  <p className="text-xs text-muted-foreground mt-1">{t.desc}</p>
                </CardContent>
              </Card>
            ))}
          </div>

          {/* Preview Panel */}
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Report Preview</CardTitle></CardHeader>
            <CardContent>
              <div className="border rounded-lg p-6 bg-white min-h-[300px]">
                <div className="text-center border-b pb-4 mb-4">
                  <h2 className="text-lg font-bold" style={{ color: PRIMARY }}>DrugOS Repurposing Report</h2>
                  <p className="text-sm text-muted-foreground">{diseases.find(d => d.id === selectedDisease)?.name} — {template.charAt(0).toUpperCase() + template.slice(1)} Report</p>
                  <p className="text-xs text-muted-foreground mt-1">Generated: {new Date().toLocaleDateString()}</p>
                </div>
                <div className="space-y-3">
                  <div><h3 className="font-semibold text-sm mb-1">Executive Summary</h3><div className="h-2 w-full bg-slate-100 rounded" /><div className="h-2 w-3/4 bg-slate-100 rounded mt-1" /></div>
                  <div><h3 className="font-semibold text-sm mb-1">Top Candidates</h3>
                    {candidates.slice(0, 3).map((c, i) => (
                      <div key={c.id} className="flex items-center gap-2 text-xs py-1">
                        <span className="font-bold text-muted-foreground">{i + 1}.</span>
                        <span className="font-medium">{c.drugName}</span>
                        <span className="text-muted-foreground">— Score: {c.compositeScore}</span>
                        <SafetyBadge tier={c.safetyTier} />
                      </div>
                    ))}
                  </div>
                  <div><h3 className="font-semibold text-sm mb-1">Methodology</h3><div className="h-2 w-full bg-slate-100 rounded" /><div className="h-2 w-5/6 bg-slate-100 rounded mt-1" /></div>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Configuration */}
        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Configuration</CardTitle></CardHeader>
            <CardContent className="space-y-4">
              <div>
                <label className="text-sm font-medium mb-1.5 block">Disease</label>
                <Select value={selectedDisease} onValueChange={setSelectedDisease}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>{diseases.map(d => <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>)}</SelectContent>
                </Select>
              </div>
              <div>
                <label className="text-sm font-medium mb-1.5 block">Candidates</label>
                <p className="text-xs text-muted-foreground">{candidates.length} candidates available</p>
              </div>
              <Button className="w-full" style={{ backgroundColor: PRIMARY }} onClick={handleGenerate} disabled={generating}>
                {generating ? <RefreshCw className="h-4 w-4 mr-2 animate-spin" /> : <FileText className="h-4 w-4 mr-2" />}
                {generating ? 'Generating...' : 'Generate PDF Report'}
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Report History</CardTitle></CardHeader>
            <CardContent className="space-y-2">
              {[
                { name: 'HD Report v2', date: '2026-06-09', type: 'Standard' },
                { name: 'AD Executive', date: '2026-06-07', type: 'Executive' },
                { name: 'PC Analysis', date: '2026-06-05', type: 'Detailed' },
              ].map((r, i) => (
                <div key={i} className="flex items-center justify-between p-2 border rounded-lg text-sm">
                  <div><span className="font-medium">{r.name}</span><br /><span className="text-xs text-muted-foreground">{r.date}</span></div>
                  <div className="flex items-center gap-2"><Badge variant="outline" className="text-xs">{r.type}</Badge><Button variant="ghost" size="sm" className="h-6 w-6 p-0"><Download className="h-3.5 w-3.5" /></Button></div>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// 10-25. ADDITIONAL SCREENS
// ═══════════════════════════════════════════

function AdvancedSearchScreen() {
  const { navigate } = useDrugOSNav();
  const [query, setQuery] = useState('');
  const [area, setArea] = useState('all');
  const [scoreMin, setScoreMin] = useState(0);
  const [phase, setPhase] = useState('all');
  const [tier, setTier] = useState('all');

  const results = useMemo(() => {
    return drugCandidates.filter(c => {
      const matchQuery = !query || c.drugName.toLowerCase().includes(query.toLowerCase()) || c.mechanism.toLowerCase().includes(query.toLowerCase());
      const disease = diseases.find(d => d.id === c.diseaseId);
      const matchArea = area === 'all' || disease?.therapeuticArea === area;
      const matchScore = c.compositeScore >= scoreMin;
      const matchPhase = phase === 'all' || c.clinicalPhase === phase;
      const matchTier = tier === 'all' || c.safetyTier === tier;
      return matchQuery && matchArea && matchScore && matchPhase && matchTier;
    });
  }, [query, area, scoreMin, phase, tier]);

  return (
    <FadeIn>
      <PageHeader title="Advanced Search" description="Multi-filter search across all drug candidates" onBack={() => navigate({ page: 'app', section: 'search' })} />
      <Card className="mb-6">
        <CardContent className="p-6 space-y-4">
          <Input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search by drug name, mechanism, target..." />
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <div><label className="text-sm font-medium mb-1.5 block">Therapeutic Area</label>
              <Select value={area} onValueChange={setArea}><SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="all">All</SelectItem>{[...new Set(diseases.map(d => d.therapeuticArea))].map(a => <SelectItem key={a} value={a}>{a}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div><label className="text-sm font-medium mb-1.5 block">Min Score: {scoreMin}</label>
              <Slider value={[scoreMin]} onValueChange={v => setScoreMin(v[0])} min={0} max={100} step={5} />
            </div>
            <div><label className="text-sm font-medium mb-1.5 block">Phase</label>
              <Select value={phase} onValueChange={setPhase}><SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="all">All</SelectItem>{[...new Set(drugCandidates.map(c => c.clinicalPhase))].map(p => <SelectItem key={p} value={p}>{p}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div><label className="text-sm font-medium mb-1.5 block">Safety Tier</label>
              <Select value={tier} onValueChange={setTier}><SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="all">All</SelectItem><SelectItem value="green">Safe</SelectItem><SelectItem value="yellow">Caution</SelectItem><SelectItem value="red">High Risk</SelectItem></SelectContent>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>
      <p className="text-sm text-muted-foreground mb-3">{results.length} results</p>
      <div className="space-y-2">
        {results.slice(0, 20).map(c => (
          <Card key={c.id} className="cursor-pointer hover:shadow-md transition-shadow" onClick={() => navigate({ page: 'app', section: 'candidate', id: c.id })}>
            <CardContent className="p-4 flex items-center gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2"><span className="font-medium">{c.drugName}</span><SafetyBadge tier={c.safetyTier} /><Badge variant="outline" className="text-xs">{c.clinicalPhase}</Badge></div>
                <p className="text-xs text-muted-foreground mt-0.5 line-clamp-1">{c.mechanism}</p>
              </div>
              <ScoreBar score={c.compositeScore} size="sm" />
            </CardContent>
          </Card>
        ))}
      </div>
    </FadeIn>
  );
}

function SavedQueriesScreen() {
  const [queries, setQueries] = useState(savedQueries);
  const { navigate } = useDrugOSNav();
  return (
    <FadeIn>
      <PageHeader title="Saved Queries" description="Manage and re-run your saved search queries" />
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader><TableRow className="bg-muted/50"><TableHead>Name</TableHead><TableHead>Disease</TableHead><TableHead>Filters</TableHead><TableHead>Results</TableHead><TableHead>Created</TableHead><TableHead></TableHead></TableRow></TableHeader>
            <TableBody>
              {queries.map(q => (
                <TableRow key={q.id} className="cursor-pointer hover:bg-muted/30" onClick={() => {
                  const disease = diseases.find(d => d.name === q.disease);
                  if (disease) navigate({ page: 'app', section: 'results', id: disease.id });
                }}>
                  <TableCell className="font-medium">{q.name}</TableCell>
                  <TableCell>{q.disease}</TableCell>
                  <TableCell><span className="text-xs text-muted-foreground">{q.filters}</span></TableCell>
                  <TableCell><Badge variant="secondary">{q.results}</Badge></TableCell>
                  <TableCell className="text-xs text-muted-foreground">{q.created}</TableCell>
                  <TableCell><Button variant="ghost" size="sm" className="h-7" onClick={e => { e.stopPropagation(); setQueries(prev => prev.filter(x => x.id !== q.id)); }}><Trash2 className="h-3.5 w-3.5 text-muted-foreground" /></Button></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </FadeIn>
  );
}

function DrugComparisonScreen() {
  const { navigate } = useDrugOSNav();
  const [selectedIds, setSelectedIds] = useState<string[]>(['DC001', 'DC002']);
  const compared = selectedIds.map(id => drugCandidates.find(c => c.id === id)).filter(Boolean) as DrugCandidate[];
  const uniqueDrugNames = [...new Set(drugCandidates.map(c => c.drugName))];

  const toggleDrug = (id: string) => {
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : prev.length < 4 ? [...prev, id] : prev);
  };

  return (
    <FadeIn>
      <PageHeader title="Drug Comparison" description="Compare up to 4 drug candidates side-by-side" />
      <Card className="mb-6">
        <CardContent className="p-4">
          <p className="text-sm font-medium mb-2">Select drugs to compare ({selectedIds.length}/4):</p>
          <div className="flex flex-wrap gap-2">
            {drugCandidates.slice(0, 13).map(c => (
              <Badge key={c.id} variant={selectedIds.includes(c.id) ? 'default' : 'outline'} className="cursor-pointer" onClick={() => toggleDrug(c.id)}>{c.drugName}</Badge>
            ))}
          </div>
        </CardContent>
      </Card>
      {compared.length > 1 && (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <Table>
              <TableHeader><TableRow className="bg-muted/50"><TableHead>Metric</TableHead>{compared.map(c => <TableHead key={c.id} className="text-center">{c.drugName}</TableHead>)}</TableRow></TableHeader>
              <TableBody>
                {[
                  { label: 'Composite Score', key: 'compositeScore' },
                  { label: 'KG Score', key: 'kgScore' },
                  { label: 'Mol Similarity', key: 'molSimScore' },
                  { label: 'Safety Score', key: 'safetyScore' },
                  { label: 'Clinical Score', key: 'clinicalScore' },
                ].map(row => (
                  <TableRow key={row.key}>
                    <TableCell className="font-medium text-sm">{row.label}</TableCell>
                    {compared.map(c => {
                      const val = (c as Record<string, unknown>)[row.key] as number;
                      const max = Math.max(...compared.map(x => (x as Record<string, unknown>)[row.key] as number));
                      return (
                        <TableCell key={c.id} className="text-center">
                          <span className={`font-bold ${val === max ? 'text-emerald-600' : ''}`}>{val}</span>
                        </TableCell>
                      );
                    })}
                  </TableRow>
                ))}
                <TableRow>
                  <TableCell className="font-medium text-sm">Safety Tier</TableCell>
                  {compared.map(c => <TableCell key={c.id} className="text-center"><SafetyBadge tier={c.safetyTier} /></TableCell>)}
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium text-sm">Phase</TableCell>
                  {compared.map(c => <TableCell key={c.id} className="text-center"><Badge variant="outline" className="text-xs">{c.clinicalPhase}</Badge></TableCell>)}
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium text-sm">IP Status</TableCell>
                  {compared.map(c => <TableCell key={c.id} className="text-center text-xs">{c.ipStatus}</TableCell>)}
                </TableRow>
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </FadeIn>
  );
}

function DrugInteractionScreen() {
  const [drug1, setDrug1] = useState(drugCandidates[0].drugName);
  const [drug2, setDrug2] = useState('');
  const uniqueDrugNames = [...new Set(drugCandidates.map(c => c.drugName))];

  const results = useMemo(() => {
    if (!drug2.trim()) return drugInteractions.filter(d => d.drug1 === drug1);
    return drugInteractions.filter(d =>
      (d.drug1 === drug1 && d.drug2.toLowerCase().includes(drug2.toLowerCase())) ||
      (d.drug2 === drug1 && d.drug1.toLowerCase().includes(drug2.toLowerCase()))
    );
  }, [drug1, drug2]);

  return (
    <FadeIn>
      <PageHeader title="Drug-Drug Interaction Checker" description="Check for interactions between medications" />
      <Card className="mb-6">
        <CardContent className="p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div><label className="text-sm font-medium mb-1.5 block">Drug 1</label>
              <Select value={drug1} onValueChange={setDrug1}><SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{uniqueDrugNames.map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent></Select>
            </div>
            <div><label className="text-sm font-medium mb-1.5 block">Drug 2 (or class)</label>
              <Input value={drug2} onChange={e => setDrug2(e.target.value)} placeholder="Enter medication or class..." /></div>
          </div>
        </CardContent>
      </Card>
      <div className="space-y-3">
        {results.length > 0 ? results.map((r, i) => (
          <Card key={i}><CardContent className="p-4">
            <div className="flex items-center gap-2 mb-2">
              <Badge variant={r.severity === 'contraindicated' ? 'destructive' : r.severity === 'major' ? 'secondary' : r.severity === 'moderate' ? 'outline' : 'secondary'} className="text-xs">{r.severity}</Badge>
              <span className="font-medium">{r.drug1} ↔ {r.drug2}</span>
            </div>
            <p className="text-sm">{r.description}</p>
            <p className="text-xs text-muted-foreground mt-1">Mechanism: {r.mechanism}</p>
          </CardContent></Card>
        )) : <Card><CardContent className="p-8 text-center"><p className="text-muted-foreground">No interactions found</p></CardContent></Card>}
      </div>
    </FadeIn>
  );
}

function MolecularSimilarityScreen() {
  const [searchDrug, setSearchDrug] = useState('Memantine');
  const results = useMemo(() => {
    return drugCandidates.map(c => ({
      ...c,
      similarity: Math.round(50 + Math.random() * 50),
    })).sort((a, b) => b.similarity - a.similarity).slice(0, 10);
  }, [searchDrug]);

  return (
    <FadeIn>
      <PageHeader title="Molecular Similarity Search" description="Find drugs with similar molecular structures" />
      <Card className="mb-6">
        <CardContent className="p-4">
          <div className="flex items-center gap-3">
            <Select value={searchDrug} onValueChange={setSearchDrug}>
              <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
              <SelectContent>{[...new Set(drugCandidates.map(c => c.drugName))].map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
            </Select>
            <Button style={{ backgroundColor: PRIMARY }}><Search className="h-4 w-4 mr-2" />Search Similar</Button>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader><TableRow className="bg-muted/50"><TableHead>Drug</TableHead><TableHead>Similarity</TableHead><TableHead>Disease</TableHead><TableHead>Composite Score</TableHead><TableHead>Safety</TableHead></TableRow></TableHeader>
            <TableBody>
              {results.map(c => (
                <TableRow key={c.id}>
                  <TableCell><span className="font-medium">{c.drugName}</span></TableCell>
                  <TableCell><ScoreBar score={c.similarity} size="sm" /></TableCell>
                  <TableCell className="text-xs">{diseases.find(d => d.id === c.diseaseId)?.name}</TableCell>
                  <TableCell>{c.compositeScore}</TableCell>
                  <TableCell><SafetyBadge tier={c.safetyTier} /></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </FadeIn>
  );
}

function ScoreBreakdownScreen() {
  const [selectedId, setSelectedId] = useState('DC001');
  const candidate = drugCandidates.find(c => c.id === selectedId) || drugCandidates[0];

  const chartData = [
    { name: 'KG Score', value: candidate.kgScore, fill: PRIMARY },
    { name: 'Mol Similarity', value: candidate.molSimScore, fill: '#3B82F6' },
    { name: 'Safety', value: candidate.safetyScore, fill: ACCENT_GREEN },
    { name: 'Clinical', value: candidate.clinicalScore, fill: ACCENT_ORANGE },
  ];

  return (
    <FadeIn>
      <PageHeader title="Composite Score Breakdown" description="Detailed score decomposition for drug candidates" />
      <div className="mb-4">
        <Select value={selectedId} onValueChange={setSelectedId}>
          <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
          <SelectContent>{drugCandidates.slice(0, 13).map(c => <SelectItem key={c.id} value={c.id}>{c.drugName}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">{candidate.drugName} — Score: {candidate.compositeScore}</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            {chartData.map(s => (
              <div key={s.name}>
                <div className="flex justify-between text-sm mb-1"><span>{s.name}</span><span className="font-bold" style={{ color: scoreColor(s.value) }}>{s.value}</span></div>
                <div className="w-full bg-slate-100 rounded-full h-3 overflow-hidden">
                  <div className="h-full rounded-full transition-all" style={{ width: `${s.value}%`, backgroundColor: s.fill }} />
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">Score Comparison Chart</CardTitle></CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" tick={{ fontSize: 12 }} />
                <YAxis domain={[0, 100]} />
                <RechartsTooltip />
                <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                  {chartData.map((entry, index) => <Cell key={index} fill={entry.fill} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      </div>
    </FadeIn>
  );
}

function DiseaseDetailScreen() {
  const { navigate, currentRoute } = useDrugOSNav();
  const diseaseId = currentRoute.id || 'D001';
  const disease = diseases.find(d => d.id === diseaseId) || diseases[0];
  const relatedCandidates = drugCandidates.filter(c => c.diseaseId === disease.id);
  const relatedTrials = clinicalTrials.filter(t => t.disease === disease.name);

  return (
    <FadeIn>
      <PageHeader title={disease.name} description={`${disease.therapeuticArea} · ICD-10: ${disease.icdCode} · ${disease.prevalence}`} onBack={() => navigate({ page: 'app', section: 'search' })} />
      <Card className="mb-6"><CardContent className="p-4"><p className="text-sm">{disease.description}</p></CardContent></Card>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
        <StatCard icon={Search} value={relatedCandidates.length} label="Drug Candidates" color={PRIMARY} />
        <StatCard icon={FlaskConical} value={relatedTrials.length} label="Clinical Trials" color={ACCENT_GREEN} />
        <StatCard icon={Activity} value={relatedCandidates.length > 0 ? Math.round(relatedCandidates.reduce((s, c) => s + c.compositeScore, 0) / relatedCandidates.length) : 0} label="Avg Score" color={ACCENT_ORANGE} />
      </div>
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Top Candidates</CardTitle></CardHeader>
        <CardContent className="space-y-2">
          {relatedCandidates.sort((a, b) => b.compositeScore - a.compositeScore).map(c => (
            <div key={c.id} className="flex items-center justify-between p-3 border rounded-lg cursor-pointer hover:bg-accent transition-colors" onClick={() => navigate({ page: 'app', section: 'candidate', id: c.id })}>
              <div className="flex items-center gap-3"><span className="font-medium">{c.drugName}</span><SafetyBadge tier={c.safetyTier} /><Badge variant="outline" className="text-xs">{c.clinicalPhase}</Badge></div>
              <ScoreBar score={c.compositeScore} size="sm" />
            </div>
          ))}
        </CardContent>
      </Card>
    </FadeIn>
  );
}

function ShortlistsScreen() {
  const [shortlists, setShortlists] = useState([
    { id: 'SL1', name: 'HD Top Picks', drugs: ['Memantine', 'Riluzole', 'Metformin'], created: '2026-06-09' },
    { id: 'SL2', name: 'AD Safe Options', drugs: ['Donepezil', 'Memantine'], created: '2026-06-07' },
    { id: 'SL3', name: 'Novel IP Opportunities', drugs: ['Cannabidiol', 'Fingolimod'], created: '2026-06-05' },
  ]);
  const { navigate } = useDrugOSNav();
  return (
    <FadeIn>
      <PageHeader title="Shortlists & Collections" description="Manage your candidate shortlists" actions={<Button style={{ backgroundColor: PRIMARY }}><Plus className="h-4 w-4 mr-2" />New Shortlist</Button>} />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {shortlists.map(sl => (
          <Card key={sl.id}>
            <CardHeader className="pb-3"><CardTitle className="text-base">{sl.name}</CardTitle><CardDescription>{sl.drugs.length} drugs · Created {sl.created}</CardDescription></CardHeader>
            <CardContent className="space-y-2">
              {sl.drugs.map(d => {
                const cand = drugCandidates.find(c => c.drugName === d);
                return (
                  <div key={d} className="flex items-center justify-between p-2 rounded-lg hover:bg-accent cursor-pointer" onClick={() => cand && navigate({ page: 'app', section: 'candidate', id: cand.id })}>
                    <span className="text-sm">{d}</span>
                    {cand && <ScoreBar score={cand.compositeScore} size="sm" />}
                  </div>
                );
              })}
              <Button variant="outline" size="sm" className="w-full mt-2" onClick={() => navigate({ page: 'app', section: 'comparison' })}><BarChart3 className="h-4 w-4 mr-1.5" />Compare</Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </FadeIn>
  );
}

function QueryHistoryScreen() {
  const { navigate } = useDrugOSNav();
  return (
    <FadeIn>
      <PageHeader title="Query History" description="Your past search history" />
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader><TableRow className="bg-muted/50"><TableHead>Date</TableHead><TableHead>Disease</TableHead><TableHead>Candidates</TableHead><TableHead>Top Score</TableHead><TableHead></TableHead></TableRow></TableHeader>
            <TableBody>
              {recentQueries.map(q => {
                const disease = diseases.find(d => d.name === q.disease);
                return (
                  <TableRow key={q.id}>
                    <TableCell className="text-sm text-muted-foreground">{q.date}</TableCell>
                    <TableCell className="font-medium">{q.disease}</TableCell>
                    <TableCell><Badge variant="secondary">{q.candidates}</Badge></TableCell>
                    <TableCell><span className="font-bold" style={{ color: scoreColor(q.topScore) }}>{q.topScore}</span></TableCell>
                    <TableCell><Button variant="ghost" size="sm" onClick={() => disease && navigate({ page: 'app', section: 'results', id: disease.id })}>Re-run</Button></TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </FadeIn>
  );
}

function BatchQueryScreen() {
  const [input, setInput] = useState("Huntington's Disease\nAlzheimer's Disease\nPancreatic Cancer");
  const [results, setResults] = useState<{ disease: string; count: number; topScore: number }[]>([]);

  const handleRun = () => {
    const lines = input.split('\n').filter(l => l.trim());
    const r = lines.map(line => {
      const disease = diseases.find(d => d.name.toLowerCase().includes(line.trim().toLowerCase()));
      const cands = drugCandidates.filter(c => c.diseaseId === disease?.id);
      return { disease: line.trim(), count: cands.length, topScore: cands.length > 0 ? Math.max(...cands.map(c => c.compositeScore)) : 0 };
    });
    setResults(r);
  };

  return (
    <FadeIn>
      <PageHeader title="Batch Query Mode" description="Run queries for multiple diseases at once" />
      <Card className="mb-6">
        <CardContent className="p-6 space-y-4">
          <label className="text-sm font-medium">Enter diseases (one per line):</label>
          <textarea value={input} onChange={e => setInput(e.target.value)} className="w-full h-32 px-3 py-2 border rounded-lg text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary/20" />
          <Button style={{ backgroundColor: PRIMARY }} onClick={handleRun}><Play className="h-4 w-4 mr-2" />Run Batch Query</Button>
        </CardContent>
      </Card>
      {results.length > 0 && (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader><TableRow className="bg-muted/50"><TableHead>Disease</TableHead><TableHead>Candidates</TableHead><TableHead>Top Score</TableHead></TableRow></TableHeader>
              <TableBody>
                {results.map((r, i) => (
                  <TableRow key={i}>
                    <TableCell className="font-medium">{r.disease}</TableCell>
                    <TableCell>{r.count}</TableCell>
                    <TableCell><span className="font-bold" style={{ color: scoreColor(r.topScore) }}>{r.topScore || 'N/A'}</span></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </FadeIn>
  );
}

function PredictionExplorerScreen() {
  const [selectedDrug, setSelectedDrug] = useState(drugCandidates[0].drugName);
  const candidate = drugCandidates.find(c => c.drugName === selectedDrug) || drugCandidates[0];

  return (
    <FadeIn>
      <PageHeader title="Prediction Explorer" description="Explore AI predictions in detail" />
      <div className="mb-4">
        <Select value={selectedDrug} onValueChange={setSelectedDrug}>
          <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
          <SelectContent>{[...new Set(drugCandidates.map(c => c.drugName))].map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
        <StatCard icon={Brain} value={candidate.compositeScore} label="AI Composite Score" color={PRIMARY} />
        <StatCard icon={Target} value={candidate.kgScore} label="Graph Prediction" color={ACCENT_GREEN} />
        <StatCard icon={Zap} value={Math.round(candidate.compositeScore * 0.85)} label="Confidence" color={ACCENT_ORANGE} />
      </div>
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Prediction Breakdown</CardTitle></CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={[
              { name: 'KG Score', value: candidate.kgScore, fill: PRIMARY },
              { name: 'Molecular', value: candidate.molSimScore, fill: '#3B82F6' },
              { name: 'Safety', value: candidate.safetyScore, fill: ACCENT_GREEN },
              { name: 'Clinical', value: candidate.clinicalScore, fill: ACCENT_ORANGE },
            ]}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="name" tick={{ fontSize: 12 }} />
              <YAxis domain={[0, 100]} />
              <RechartsTooltip />
              <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                {[PRIMARY, '#3B82F6', ACCENT_GREEN, ACCENT_ORANGE].map((c, i) => <Cell key={i} fill={c} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    </FadeIn>
  );
}

function EvidenceTimelineScreen() {
  const evidence = evidenceItems.sort((a, b) => b.year - a.year);
  return (
    <FadeIn>
      <PageHeader title="Evidence Timeline" description="Timeline of evidence for drug-disease pairs" />
      <div className="relative">
        <div className="absolute left-6 top-0 bottom-0 w-0.5 bg-border" />
        <div className="space-y-6">
          {evidence.map((ev, i) => (
            <div key={ev.id} className="relative pl-14">
              <div className="absolute left-4 w-5 h-5 rounded-full border-2 bg-background" style={{ borderColor: ev.type === 'clinical' ? ACCENT_GREEN : ev.type === 'preclinical' ? PRIMARY : ACCENT_ORANGE }} />
              <Card><CardContent className="p-4">
                <div className="flex items-center gap-2 mb-1"><Badge variant="secondary" className="text-[10px]">{ev.type}</Badge><span className="text-xs text-muted-foreground">{ev.year}</span><span className="font-medium text-sm">{ev.drugName}</span></div>
                <p className="text-sm font-medium">{ev.title}</p>
                <p className="text-xs text-muted-foreground mt-1">{ev.source} · Quality: {ev.quality}</p>
              </CardContent></Card>
            </div>
          ))}
        </div>
      </div>
    </FadeIn>
  );
}

function MechanismOfActionScreen() {
  const [selectedDrug, setSelectedDrug] = useState(drugCandidates[0].drugName);
  const candidate = drugCandidates.find(c => c.drugName === selectedDrug) || drugCandidates[0];
  const disease = diseases.find(d => d.id === candidate.diseaseId);

  return (
    <FadeIn>
      <PageHeader title="Mechanism of Action" description="Detailed MoA view for drug candidates" />
      <div className="mb-4">
        <Select value={selectedDrug} onValueChange={setSelectedDrug}>
          <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
          <SelectContent>{[...new Set(drugCandidates.map(c => c.drugName))].map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">{candidate.drugName} Mechanism</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm">{candidate.mechanism}</p>
            <div><span className="text-xs font-semibold text-muted-foreground">Target Proteins</span>
              <div className="flex flex-wrap gap-2 mt-1">{candidate.targets.map(t => <Badge key={t} variant="secondary" className="font-mono">{t}</Badge>)}</div></div>
            <div><span className="text-xs font-semibold text-muted-foreground">Pathways</span>
              <div className="flex flex-wrap gap-2 mt-1">{candidate.pathways.map(p => <Badge key={p} variant="outline">{p}</Badge>)}</div></div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">Pathway Diagram</CardTitle></CardHeader>
          <CardContent><PathwayDiagram candidate={candidate} disease={disease || diseases[0]} /></CardContent>
        </Card>
      </div>
    </FadeIn>
  );
}

function RegulatoryPathwayScreen() {
  const [selectedDrug, setSelectedDrug] = useState(drugCandidates[0].drugName);
  const candidate = drugCandidates.find(c => c.drugName === selectedDrug) || drugCandidates[0];

  return (
    <FadeIn>
      <PageHeader title="Regulatory Pathway Assessment" description="Assess regulatory requirements for drug repurposing" />
      <div className="mb-4">
        <Select value={selectedDrug} onValueChange={setSelectedDrug}>
          <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
          <SelectContent>{[...new Set(drugCandidates.map(c => c.drugName))].map(d => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">Regulatory Steps</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            {[
              { step: 'Pre-IND Meeting', status: 'required', desc: 'Request Type B meeting with FDA' },
              { step: 'IND Application', status: 'required', desc: 'Submit 505(b)(2) application' },
              { step: 'Phase II Trial', status: candidate.clinicalPhase === 'Phase II' || candidate.clinicalPhase === 'Phase III' ? 'complete' : 'pending', desc: 'Confirmatory efficacy study' },
              { step: 'Phase III Trial', status: candidate.clinicalPhase === 'Phase III' ? 'complete' : 'pending', desc: 'Pivotal registration trial' },
              { step: 'NDA Submission', status: 'pending', desc: '505(b)(2) NDA filing' },
              { step: 'FDA Review', status: 'pending', desc: 'Standard 10-12 month review' },
            ].map((s, i) => (
              <div key={i} className="flex items-start gap-3 p-3 border rounded-lg">
                <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold shrink-0 ${s.status === 'complete' ? 'bg-emerald-100 text-emerald-700' : s.status === 'required' ? 'bg-primary/10 text-primary' : 'bg-slate-100 text-slate-400'}`}>
                  {s.status === 'complete' ? '✓' : i + 1}
                </div>
                <div><span className="font-medium text-sm">{s.step}</span><p className="text-xs text-muted-foreground">{s.desc}</p></div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3"><CardTitle className="text-base">Regulatory Considerations</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <div className="p-3 bg-primary/5 border border-primary/20 rounded-lg">
              <h4 className="font-medium text-sm mb-1">505(b)(2) Pathway</h4>
              <p className="text-xs text-muted-foreground">This drug may qualify for the 505(b)(2) abbreviated NDA pathway since it is already FDA-approved for another indication.</p>
            </div>
            <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg">
              <h4 className="font-medium text-sm mb-1">Orphan Drug Status</h4>
              <p className="text-xs text-muted-foreground">{diseases.find(d => d.id === candidate.diseaseId)?.prevalence?.includes('per 100,000') ? 'May qualify for orphan drug designation' : 'Prevalence may not meet orphan drug criteria'}</p>
            </div>
          </CardContent>
        </Card>
      </div>
    </FadeIn>
  );
}

// ═══════════════════════════════════════════
// EXPORT
// ═══════════════════════════════════════════

export const coreScreens: Record<string, React.ComponentType> = {
  'search': DiseaseSearchScreen,
  'results': SearchResultsScreen,
  'candidate': CandidateDetailScreen,
  'knowledge-graph': KnowledgeGraphScreen,
  'clinical-trials': ClinicalTrialsScreen,
  'safety': SafetyProfileScreen,
  'ip-patents': IPPatentsScreen,
  'evidence-builder': EvidenceBuilderScreen,
  'reports': ReportGenerationScreen,
  'advanced-search': AdvancedSearchScreen,
  'saved-queries': SavedQueriesScreen,
  'comparison': DrugComparisonScreen,
  'interactions': DrugInteractionScreen,
  'molecular-similarity': MolecularSimilarityScreen,
  'score-breakdown': ScoreBreakdownScreen,
  'disease-detail': DiseaseDetailScreen,
  'shortlists': ShortlistsScreen,
  'history': QueryHistoryScreen,
  'batch-query': BatchQueryScreen,
  'prediction-explorer': PredictionExplorerScreen,
  'evidence-timeline': EvidenceTimelineScreen,
  'mechanism': MechanismOfActionScreen,
  'regulatory': RegulatoryPathwayScreen,
  ...remainingScreens,
};
