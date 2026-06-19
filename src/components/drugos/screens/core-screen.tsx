'use client';

import { useState } from 'react';
import {
  Search,
  Filter,
  Download,
  Columns3,
  List,
  Package,
  FileBarChart,
  ShieldCheck,
  AlertTriangle,
} from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { DiseaseSearchBar } from '@/components/drugos/disease-search-bar';
import { CandidateTable } from '@/components/drugos/candidate-table';
import { ScoreBar } from '@/components/drugos/score-bar';
import { SafetyBadge } from '@/components/drugos/safety-badge';
import { KnowledgeGraphViewer } from '@/components/drugos/knowledge-graph-viewer';
import { PathwayViz } from '@/components/drugos/pathway-viz';
import { TabLayout } from '@/components/drugos/tab-layout';
import { StatCard } from '@/components/drugos/stat-card';
import { getScreenMeta } from '@/lib/screens';
import {
  diseases,
  drugCandidates,
  clinicalTrials,
  patents,
  getScoreBreakdown,
} from '@/lib/mock-data';

interface CoreScreenProps {
  screenId: string;
}

export function CoreScreen({ screenId }: CoreScreenProps) {
  switch (screenId) {
    case 'CORE-01':
      return <DiseaseSearchHome />;
    case 'CORE-02':
      return <SearchResults />;
    case 'CORE-03':
      return <CandidateDetail />;
    case 'CORE-04':
      return <MechanisticPathway />;
    case 'CORE-05':
      return <SafetyProfile />;
    case 'CORE-06':
      return <KnowledgeGraphScreen />;
    case 'CORE-07':
      return <ClinicalTrialSearch />;
    case 'CORE-08':
      return <IPPatentStatus />;
    case 'CORE-09':
      return <EvidenceBuilder />;
    case 'CORE-10':
      return <ReportGeneration />;
    case 'CORE-21':
      return <ScoreBreakdown />;
    case 'CORE-22':
      return <DiseaseDetail />;
    default:
      return <CorePlaceholderScreen screenId={screenId} />;
  }
}

// ---- CORE-01: Disease Search Home ----

function DiseaseSearchHome() {
  const [selectedDisease, setSelectedDisease] = useState<string | null>(null);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-foreground">Disease Search</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Search for diseases to discover drug repurposing candidates
        </p>
      </div>

      {/* Hero Search */}
      <Card className="bg-gradient-to-r from-primary/5 to-primary/10 border-primary/20">
        <CardContent className="p-6">
          <h2 className="text-lg font-semibold mb-1">What disease are you researching?</h2>
          <p className="text-sm text-muted-foreground mb-4">Enter a disease name, gene, or ICD-10 code to start</p>
          <DiseaseSearchBar
            onDiseaseSelect={(id) => setSelectedDisease(id)}
            className="max-w-xl"
          />
        </CardContent>
      </Card>

      {/* Disease Categories */}
      <div>
        <h3 className="text-base font-semibold mb-3">Browse by Category</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {diseases.map((disease) => (
            <Card
              key={disease.id}
              className="cursor-pointer hover:shadow-md hover:border-primary/30 transition-all"
              onClick={() => setSelectedDisease(disease.id)}
            >
              <CardContent className="p-4">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="font-medium text-sm">{disease.name}</h4>
                  <Badge variant="secondary" className="text-[10px]">{disease.icd10}</Badge>
                </div>
                <p className="text-xs text-muted-foreground line-clamp-2 mb-3">{disease.description}</p>
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span className="flex items-center gap-1">
                    <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    {disease.candidateCount} candidates
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="h-1.5 w-1.5 rounded-full bg-[#1D9E75]" />
                    {disease.clinicalTrialCount} trials
                  </span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---- CORE-02: Search Results ----

function SearchResults() {
  const [filterTier, setFilterTier] = useState<string>('all');

  const filtered = filterTier === 'all'
    ? drugCandidates
    : drugCandidates.filter((c) => c.safetyTier === filterTier);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Search Results</h1>
          <p className="text-sm text-muted-foreground mt-1">{drugCandidates.length} drug candidates found</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm">
            <Download className="h-4 w-4 mr-1.5" /> Export
          </Button>
          <Button variant="outline" size="sm">
            <Columns3 className="h-4 w-4 mr-1.5" /> Compare
          </Button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-2 flex-wrap">
        <Badge
          variant={filterTier === 'all' ? 'default' : 'outline'}
          className="cursor-pointer"
          onClick={() => setFilterTier('all')}
        >
          All ({drugCandidates.length})
        </Badge>
        <Badge
          variant={filterTier === 'green' ? 'default' : 'outline'}
          className="cursor-pointer"
          onClick={() => setFilterTier('green')}
        >
          🟢 Green ({drugCandidates.filter(c => c.safetyTier === 'green').length})
        </Badge>
        <Badge
          variant={filterTier === 'yellow' ? 'default' : 'outline'}
          className="cursor-pointer"
          onClick={() => setFilterTier('yellow')}
        >
          🟡 Yellow ({drugCandidates.filter(c => c.safetyTier === 'yellow').length})
        </Badge>
        <Badge
          variant={filterTier === 'red' ? 'default' : 'outline'}
          className="cursor-pointer"
          onClick={() => setFilterTier('red')}
        >
          🔴 Red ({drugCandidates.filter(c => c.safetyTier === 'red').length})
        </Badge>
      </div>

      {/* Results Table */}
      <CandidateTable candidates={filtered} showDiseaseColumn />
    </div>
  );
}

// ---- CORE-03: Candidate Detail ----

function CandidateDetail() {
  const candidate = drugCandidates[0]; // Memantine
  const breakdown = getScoreBreakdown(candidate.id);

  const tabs = [
    {
      id: 'overview',
      label: 'Overview',
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-4">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Drug Information</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div><span className="text-muted-foreground">Generic Name:</span> <span className="font-medium">{candidate.genericName}</span></div>
                  <div><span className="text-muted-foreground">DrugBank ID:</span> <span className="font-medium font-mono">{candidate.drugBankId}</span></div>
                  <div><span className="text-muted-foreground">Formula:</span> <span className="font-medium">{candidate.formula}</span></div>
                  <div><span className="text-muted-foreground">Mol. Weight:</span> <span className="font-medium">{candidate.molecularWeight}</span></div>
                  <div><span className="text-muted-foreground">FDA Approved:</span> <Badge variant={candidate.fdaApproved ? 'default' : 'secondary'}>{candidate.fdaApproved ? `Yes (${candidate.yearApproved})` : 'No'}</Badge></div>
                  <div><span className="text-muted-foreground">Patent Status:</span> <Badge variant="outline">{candidate.patentStatus}</Badge></div>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Mechanism of Action</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm">{candidate.mechanism}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {candidate.targetGenes.map((gene) => (
                    <Badge key={gene} variant="secondary">{gene}</Badge>
                  ))}
                </div>
              </CardContent>
            </Card>
          </div>
          <div className="space-y-4">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Composite Score</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <ScoreBar score={candidate.compositeScore} size="lg" />
                <div className="space-y-2">
                  {Object.entries(breakdown.components).map(([key, val]) => (
                    <div key={key}>
                      <div className="flex justify-between text-xs mb-0.5">
                        <span className="capitalize text-muted-foreground">{key.replace(/([A-Z])/g, ' $1')}</span>
                        <span className="font-medium">{val}</span>
                      </div>
                      <ScoreBar score={val} size="sm" showLabel={false} />
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Safety Profile</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex items-center gap-2 mb-3">
                  <SafetyBadge tier={candidate.safetyTier} />
                  <span className="text-sm text-muted-foreground">Repurposing confidence: {candidate.repurposingConfidence}%</span>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      ),
    },
    {
      id: 'pathway',
      label: 'Pathway',
      badge: candidate.pathways.length,
      content: (
        <div>
          <h3 className="text-base font-semibold mb-3">Pathways</h3>
          <div className="flex flex-wrap gap-2 mb-4">
            {candidate.pathways.map((p) => (
              <Badge key={p} variant="outline">{p}</Badge>
            ))}
          </div>
          <PathwayViz />
        </div>
      ),
    },
    {
      id: 'safety',
      label: 'Safety',
      content: (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Adverse Effects</CardTitle></CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-2">
                {candidate.adverseEffects.map((e) => (
                  <Badge key={e} variant="secondary" className="text-[#D4853A]">{e}</Badge>
                ))}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-3"><CardTitle className="text-base">Contraindications</CardTitle></CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-2">
                {candidate.contraindications.map((c) => (
                  <Badge key={c} variant="destructive" className="text-[#C0392B]">{c}</Badge>
                ))}
              </div>
            </CardContent>
          </Card>
          <Card className="md:col-span-2">
            <CardHeader className="pb-3"><CardTitle className="text-base">Drug Interactions</CardTitle></CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-2">
                {candidate.drugInteractions.map((d) => (
                  <Badge key={d} variant="outline">{d}</Badge>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>
      ),
    },
    {
      id: 'clinical',
      label: 'Clinical',
      badge: clinicalTrials.filter(t => t.condition === candidate.diseaseName).length,
      content: (
        <div className="space-y-3">
          {clinicalTrials.filter(t => t.condition === candidate.diseaseName).map((trial) => (
            <Card key={trial.id}>
              <CardContent className="p-4">
                <div className="flex items-start justify-between">
                  <div>
                    <h4 className="font-medium text-sm">{trial.title}</h4>
                    <div className="flex items-center gap-2 mt-1">
                      <Badge variant="outline" className="text-xs">{trial.nctId}</Badge>
                      <Badge variant="secondary" className="text-xs">{trial.phase}</Badge>
                      <Badge className="text-xs">{trial.status}</Badge>
                    </div>
                    <p className="text-xs text-muted-foreground mt-2">
                      Sponsor: {trial.sponsor} · Enrollment: {trial.enrollment} · {trial.startDate} – {trial.completionDate}
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ),
    },
    {
      id: 'evidence',
      label: 'Evidence',
      badge: candidate.evidenceCount,
      content: (
        <Card>
          <CardHeader><CardTitle className="text-base">Evidence Summary</CardTitle></CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">{candidate.evidenceCount} pieces of evidence supporting {candidate.name} for {candidate.diseaseName}</p>
            <div className="mt-4 space-y-3">
              {Array.from({length: Math.min(5, candidate.evidenceCount)}, (_, i) => (
                <div key={i} className="p-3 border border-border rounded-lg">
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary" className="text-[10px]">Source {(i % 3) + 1}</Badge>
                    <span className="text-sm font-medium">Evidence Item {i + 1}</span>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">Supporting data from clinical and preclinical studies</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      ),
    },
    {
      id: 'ip',
      label: 'IP',
      badge: patents.filter(p => p.relatedDrugId === candidate.id).length,
      content: (
        <div className="space-y-3">
          {patents.filter(p => p.relatedDrugId === candidate.id).map((pat) => (
            <Card key={pat.id}>
              <CardContent className="p-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-medium text-sm">{pat.title}</span>
                  <Badge variant={pat.status === 'active' ? 'default' : pat.status === 'expired' ? 'secondary' : 'outline'}>
                    {pat.status}
                  </Badge>
                </div>
                <div className="text-xs text-muted-foreground space-y-0.5">
                  <p>{pat.patentNumber} · {pat.jurisdiction} · {pat.claims} claims</p>
                  <p>Assignee: {pat.assignee}</p>
                  <p>Filed: {pat.filingDate} · Exp: {pat.expirationDate}</p>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <div>
          <h1 className="text-2xl font-bold">{candidate.name}</h1>
          <p className="text-sm text-muted-foreground mt-0.5">{candidate.genericName} · {candidate.diseaseName}</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <SafetyBadge tier={candidate.safetyTier} />
          <Badge variant="outline">{candidate.phase}</Badge>
        </div>
      </div>

      <TabLayout tabs={tabs} defaultTab="overview" />
    </div>
  );
}

// ---- CORE-04: Mechanistic Pathway ----

function MechanisticPathway() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Mechanistic Pathway Visualization</h1>
        <p className="text-sm text-muted-foreground mt-1">Interactive pathway diagrams for drug mechanisms</p>
      </div>
      <PathwayViz className="w-full" />
      <Card>
        <CardHeader><CardTitle className="text-base">Pathway Details</CardTitle></CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div><span className="text-muted-foreground">Nodes:</span> <span className="font-medium">10</span></div>
            <div><span className="text-muted-foreground">Edges:</span> <span className="font-medium">10</span></div>
            <div><span className="text-muted-foreground">Activations:</span> <span className="font-medium text-[#1D9E75]">8</span></div>
            <div><span className="text-muted-foreground">Inhibitions:</span> <span className="font-medium text-[#C0392B]">2</span></div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- CORE-05: Safety Profile Dashboard ----

function SafetyProfile() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Safety Profile Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Comprehensive safety analysis across all candidates</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard icon={ShieldCheck} value={89} label="Green (Safe)" iconColor="text-[#1D9E75]" />
        <StatCard icon={AlertTriangle} value={124} label="Yellow (Caution)" iconColor="text-[#D4853A]" />
        <StatCard icon={AlertTriangle} value={61} label="Red (High Risk)" iconColor="text-[#C0392B]" />
      </div>
      <CandidateTable candidates={drugCandidates.filter(c => c.safetyTier === 'red')} showDiseaseColumn />
    </div>
  );
}

// ---- CORE-06: Knowledge Graph ----

function KnowledgeGraphScreen() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Knowledge Graph Explorer</h1>
        <p className="text-sm text-muted-foreground mt-1">Explore relationships between diseases, drugs, genes, and pathways</p>
      </div>
      <KnowledgeGraphViewer className="w-full" height={500} />
    </div>
  );
}

// ---- CORE-07: Clinical Trial Search ----

function ClinicalTrialSearch() {
  const [search, setSearch] = useState('');
  const filtered = clinicalTrials.filter(
    (t) =>
      t.title.toLowerCase().includes(search.toLowerCase()) ||
      t.condition.toLowerCase().includes(search.toLowerCase()) ||
      t.nctId.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Clinical Trial Search</h1>
        <p className="text-sm text-muted-foreground mt-1">Search and filter clinical trials from ClinicalTrials.gov</p>
      </div>
      <div className="relative max-w-md">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search trials by title, condition, or NCT ID..."
          className="pl-9"
        />
      </div>
      <div className="space-y-3">
        {filtered.map((trial) => (
          <Card key={trial.id}>
            <CardContent className="p-4">
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <h4 className="font-medium text-sm">{trial.title}</h4>
                  <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                    <Badge variant="outline" className="text-xs font-mono">{trial.nctId}</Badge>
                    <Badge variant="secondary" className="text-xs">{trial.phase}</Badge>
                    <Badge className="text-xs">{trial.status}</Badge>
                  </div>
                  <p className="text-xs text-muted-foreground mt-2">
                    {trial.sponsor} · Enrollment: {trial.enrollment} · {trial.startDate} – {trial.completionDate}
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

// ---- CORE-08: IP Patent Status ----

function IPPatentStatus() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">IP & Patent Status</h1>
        <p className="text-sm text-muted-foreground mt-1">Track intellectual property and patent status for candidates</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <StatCard icon={Search} value={patents.filter(p => p.status === 'active').length} label="Active Patents" />
        <StatCard icon={Filter} value={patents.filter(p => p.status === 'pending').length} label="Pending" />
        <StatCard icon={Download} value={patents.filter(p => p.status === 'expired').length} label="Expired" />
        <StatCard icon={List} value={patents.filter(p => p.status === 'abandoned').length} label="Abandoned" />
      </div>
      <div className="space-y-3">
        {patents.map((pat) => (
          <Card key={pat.id}>
            <CardContent className="p-4">
              <div className="flex items-center justify-between mb-2">
                <span className="font-medium text-sm">{pat.title}</span>
                <Badge variant={pat.status === 'active' ? 'default' : pat.status === 'expired' ? 'secondary' : pat.status === 'pending' ? 'outline' : 'destructive'}>
                  {pat.status}
                </Badge>
              </div>
              <div className="text-xs text-muted-foreground space-y-0.5">
                <p>{pat.patentNumber} · {pat.jurisdiction} · {pat.claims} claims</p>
                <p>Assignee: {pat.assignee}</p>
                <p>Filed: {pat.filingDate} · Exp: {pat.expirationDate}</p>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

// ---- CORE-09: Evidence Builder ----

function EvidenceBuilder() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Evidence Package Builder</h1>
        <p className="text-sm text-muted-foreground mt-1">Build comprehensive evidence packages for drug repurposing</p>
      </div>
      <Card>
        <CardContent className="p-6">
          <div className="text-center py-12">
            <Package className="h-12 w-12 text-muted-foreground/30 mx-auto mb-4" />
            <h3 className="text-lg font-semibold mb-1">Create New Evidence Package</h3>
            <p className="text-sm text-muted-foreground mb-4">Select a drug candidate to start building an evidence package</p>
            <Button>Create Evidence Package</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- CORE-10: Report Generation ----

function ReportGeneration() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Report Generation</h1>
        <p className="text-sm text-muted-foreground mt-1">Generate and preview repurposing reports</p>
      </div>
      <Card>
        <CardContent className="p-6">
          <div className="text-center py-12">
            <FileBarChart className="h-12 w-12 text-muted-foreground/30 mx-auto mb-4" />
            <h3 className="text-lg font-semibold mb-1">Generate New Report</h3>
            <p className="text-sm text-muted-foreground mb-4">Create a detailed repurposing analysis report</p>
            <Button>Generate Report</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---- CORE-21: Score Breakdown ----

function ScoreBreakdown() {
  const candidate = drugCandidates[0];
  const breakdown = getScoreBreakdown(candidate.id);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Composite Score Breakdown</h1>
        <p className="text-sm text-muted-foreground mt-1">Detailed view of how candidate scores are calculated</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{candidate.name} — Score: {breakdown.overall}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {Object.entries(breakdown.components).map(([key, val]) => (
            <div key={key}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm capitalize">{key.replace(/([A-Z])/g, ' $1')}</span>
                <span className="text-sm font-bold">{val}</span>
              </div>
              <ScoreBar score={val} size="md" showLabel={false} />
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

// ---- CORE-22: Disease Detail ----

function DiseaseDetail() {
  const disease = diseases[0];
  const relatedCandidates = drugCandidates.filter(c => c.diseaseId === disease.id);
  const relatedTrials = clinicalTrials.filter(t => t.condition === disease.name);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{disease.name}</h1>
          <p className="text-sm text-muted-foreground mt-0.5">{disease.category} · {disease.icd10} · ORPHA: {disease.orphaCode}</p>
        </div>
        <Badge variant="outline">Prevalence: {disease.prevalence}</Badge>
      </div>

      <Card>
        <CardContent className="p-4">
          <p className="text-sm">{disease.description}</p>
          <div className="flex gap-2 mt-3">
            {disease.synonyms.map((s) => (
              <Badge key={s} variant="secondary">{s}</Badge>
            ))}
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard icon={Search} value={relatedCandidates.length} label="Drug Candidates" />
        <StatCard icon={Filter} value={relatedTrials.length} label="Clinical Trials" />
        <StatCard icon={List} value={disease.evidenceCount} label="Evidence Items" />
      </div>

      {relatedCandidates.length > 0 && (
        <CandidateTable candidates={relatedCandidates} showDiseaseColumn={false} />
      )}
    </div>
  );
}

// ---- Placeholder ----

function CorePlaceholderScreen({ screenId }: { screenId: string }) {
  const meta = getScreenMeta(screenId);
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{meta?.name ?? screenId}</h1>
        <p className="text-sm text-muted-foreground mt-1">{meta?.description ?? ''}</p>
      </div>
      <Card>
        <CardContent className="p-8 text-center">
          <p className="text-muted-foreground">This screen ({screenId}) is under development.</p>
        </CardContent>
      </Card>
    </div>
  );
}
