// DrugOS Mock Data - Comprehensive dataset for all 232 screens

export interface Disease {
  id: string;
  name: string;
  icdCode: string;
  meshTerm: string;
  therapeuticArea: string;
  prevalence: string;
  description: string;
  geneticBasis: boolean;
}

export interface DrugCandidate {
  id: string;
  drugName: string;
  brandNames: string[];
  genericName: string;
  compositeScore: number;
  kgScore: number;
  molSimScore: number;
  safetyScore: number;
  clinicalScore: number;
  safetyTier: 'green' | 'yellow' | 'red';
  mechanism: string;
  clinicalPhase: string;
  ipStatus: string;
  diseaseId: string;
  targets: string[];
  pathways: string[];
}

export interface ClinicalTrial {
  id: string;
  nctId: string;
  title: string;
  phase: string;
  status: string;
  enrollment: number;
  startDate: string;
  completionDate: string;
  drugName: string;
  disease: string;
  outcome: string;
}

export interface GraphNode {
  id: string;
  label: string;
  type: 'drug' | 'disease' | 'gene' | 'protein' | 'pathway';
  x: number;
  y: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  relation: string;
  evidence: number;
}

export interface User {
  id: string;
  name: string;
  email: string;
  role: string;
  department: string;
  status: 'active' | 'invited' | 'suspended';
  lastActive: string;
  avatar?: string;
}

export interface Notification {
  id: string;
  title: string;
  message: string;
  type: 'info' | 'warning' | 'success' | 'error';
  time: string;
  read: boolean;
}

export interface AuditLogEntry {
  id: string;
  action: string;
  user: string;
  resource: string;
  timestamp: string;
  ip: string;
  details: string;
}

// === DISEASES ===
export const diseases: Disease[] = [
  { id: 'D001', name: "Huntington's Disease", icdCode: 'G10', meshTerm: 'Huntington Disease', therapeuticArea: 'Neurology', prevalence: '5-10 per 100,000', description: 'A progressive neurodegenerative autosomal dominant disorder caused by CAG trinucleotide expansion in the HTT gene.', geneticBasis: true },
  { id: 'D002', name: "Alzheimer's Disease", icdCode: 'G30', meshTerm: 'Alzheimer Disease', therapeuticArea: 'Neurology', prevalence: '24 million globally', description: 'A progressive neurodegenerative disease characterized by amyloid plaques and neurofibrillary tangles.', geneticBasis: true },
  { id: 'D003', name: 'ALS (Lou Gehrig\'s Disease)', icdCode: 'G12.2', meshTerm: 'Amyotrophic Lateral Sclerosis', therapeuticArea: 'Neurology', prevalence: '2-5 per 100,000', description: 'A progressive motor neuron disease leading to muscle weakness and paralysis.', geneticBasis: true },
  { id: 'D004', name: "Parkinson's Disease", icdCode: 'G20', meshTerm: 'Parkinson Disease', therapeuticArea: 'Neurology', prevalence: '10 million globally', description: 'A neurodegenerative disorder characterized by dopaminergic neuron loss in the substantia nigra.', geneticBasis: true },
  { id: 'D005', name: 'Cystic Fibrosis', icdCode: 'E84', meshTerm: 'Cystic Fibrosis', therapeuticArea: 'Pulmonology', prevalence: '70,000 globally', description: 'An autosomal recessive disorder caused by CFTR gene mutations affecting multiple organ systems.', geneticBasis: true },
  { id: 'D006', name: 'Pancreatic Cancer', icdCode: 'C25', meshTerm: 'Pancreatic Neoplasms', therapeuticArea: 'Oncology', prevalence: '495,000 globally', description: 'One of the most lethal cancers with 5-year survival rate below 10%.', geneticBasis: true },
  { id: 'D007', name: 'Sickle Cell Disease', icdCode: 'D57', meshTerm: 'Sickle Cell Disease', therapeuticArea: 'Hematology', prevalence: '300,000 births/year', description: 'An inherited blood disorder caused by a mutation in the HBB gene.', geneticBasis: true },
  { id: 'D008', name: 'Multiple Sclerosis', icdCode: 'G35', meshTerm: 'Multiple Sclerosis', therapeuticArea: 'Neurology', prevalence: '2.8 million globally', description: 'An autoimmune demyelinating disease of the central nervous system.', geneticBasis: true },
  { id: 'D009', name: 'Glioblastoma', icdCode: 'C71.9', meshTerm: 'Glioblastoma', therapeuticArea: 'Oncology', prevalence: '3 per 100,000', description: 'The most aggressive primary brain tumor with median survival of 15 months.', geneticBasis: true },
  { id: 'D010', name: 'Pulmonary Fibrosis', icdCode: 'J84.1', meshTerm: 'Pulmonary Fibrosis', therapeuticArea: 'Pulmonology', prevalence: '13-20 per 100,000', description: 'A progressive lung disease characterized by scarring of lung tissue.', geneticBasis: false },
];

// === DRUG CANDIDATES ===
export const drugCandidates: DrugCandidate[] = [
  { id: 'DC001', drugName: 'Memantine', brandNames: ['Namenda'], genericName: 'memantine hydrochloride', compositeScore: 87, kgScore: 91, molSimScore: 82, safetyScore: 94, clinicalScore: 79, safetyTier: 'green', mechanism: 'NMDA receptor antagonist reducing excitotoxicity in Huntington\'s neurons', clinicalPhase: 'Phase II', ipStatus: 'Patent Expired', diseaseId: 'D001', targets: ['GRIN2A', 'GRIN2B'], pathways: ['Glutamatergic synapse', 'Calcium signaling'] },
  { id: 'DC002', drugName: 'Riluzole', brandNames: ['Rilutek', 'Teglutik'], genericName: 'riluzole', compositeScore: 84, kgScore: 88, molSimScore: 78, safetyScore: 89, clinicalScore: 81, safetyTier: 'green', mechanism: 'Glutamate release inhibitor with anti-excitotoxic properties for HD neuroprotection', clinicalPhase: 'Phase II', ipStatus: 'Patent Expired', diseaseId: 'D001', targets: ['SLC1A2', 'SLC1A3'], pathways: ['Glutamatergic synapse', 'Astrocyte-neuron metabolism'] },
  { id: 'DC003', drugName: 'Dexamethasone', brandNames: ['Decadron', 'DexPak'], genericName: 'dexamethasone', compositeScore: 82, kgScore: 79, molSimScore: 71, safetyScore: 88, clinicalScore: 90, safetyTier: 'yellow', mechanism: 'Glucocorticoid receptor agonist modulating neuroinflammation in HD', clinicalPhase: 'Phase III', ipStatus: 'Off-Patent', diseaseId: 'D001', targets: ['NR3C1', 'NFKB1'], pathways: ['Glucocorticoid signaling', 'NF-kB pathway'] },
  { id: 'DC004', drugName: 'Metformin', brandNames: ['Glucophage', 'Fortamet'], genericName: 'metformin hydrochloride', compositeScore: 79, kgScore: 74, molSimScore: 68, safetyScore: 95, clinicalScore: 80, safetyTier: 'green', mechanism: 'AMPK activator promoting autophagy and mitochondrial function in HD neurons', clinicalPhase: 'Phase II', ipStatus: 'Off-Patent', diseaseId: 'D001', targets: ['AMPK', 'PRKAA1'], pathways: ['AMPK signaling', 'Autophagy'] },
  { id: 'DC005', drugName: 'Cannabidiol', brandNames: ['Epidiolex'], genericName: 'cannabidiol', compositeScore: 76, kgScore: 72, molSimScore: 65, safetyScore: 82, clinicalScore: 85, safetyTier: 'green', mechanism: 'CB1/CB2 receptor modulator with neuroprotective anti-inflammatory effects', clinicalPhase: 'Phase I', ipStatus: 'Novel Use Patentable', diseaseId: 'D001', targets: ['CNR1', 'CNR2'], pathways: ['Endocannabinoid signaling', 'Neuroinflammation'] },
  { id: 'DC006', drugName: 'Prazosin', brandNames: ['Minipress'], genericName: 'prazosin hydrochloride', compositeScore: 73, kgScore: 68, molSimScore: 61, safetyScore: 91, clinicalScore: 72, safetyTier: 'green', mechanism: 'Alpha-1 adrenergic antagonist reducing excitatory neurotransmission in HD', clinicalPhase: 'Preclinical', ipStatus: 'Off-Patent', diseaseId: 'D001', targets: ['ADRA1A', 'ADRA1B'], pathways: ['Adrenergic signaling', 'Noradrenergic pathway'] },
  { id: 'DC007', drugName: 'Lithium Carbonate', brandNames: ['Lithobid'], genericName: 'lithium carbonate', compositeScore: 71, kgScore: 66, molSimScore: 59, safetyScore: 74, clinicalScore: 82, safetyTier: 'yellow', mechanism: 'GSK-3beta inhibitor promoting autophagy and neuroprotection via mTOR-independent pathway', clinicalPhase: 'Phase II', ipStatus: 'Off-Patent', diseaseId: 'D001', targets: ['GSK3B', 'INSR'], pathways: ['Wnt signaling', 'Autophagy'] },
  { id: 'DC008', drugName: 'Fingolimod', brandNames: ['Gilenya'], genericName: 'fingolimod', compositeScore: 69, kgScore: 65, molSimScore: 72, safetyScore: 71, clinicalScore: 68, safetyTier: 'yellow', mechanism: 'S1P receptor modulator with neuroprotective and anti-inflammatory properties', clinicalPhase: 'Phase I', ipStatus: 'Patent Active', diseaseId: 'D001', targets: ['S1PR1', 'S1PR3'], pathways: ['Sphingolipid signaling', 'Lymphocyte trafficking'] },
  { id: 'DC009', drugName: 'Ursodiol', brandNames: ['Actigall', 'Urso'], genericName: 'ursodeoxycholic acid', compositeScore: 67, kgScore: 62, molSimScore: 55, safetyScore: 96, clinicalScore: 57, safetyTier: 'green', mechanism: 'Bile acid with anti-apoptotic and mitochondrial protective effects in neurons', clinicalPhase: 'Preclinical', ipStatus: 'Off-Patent', diseaseId: 'D001', targets: ['TGR5', 'FXR'], pathways: ['Bile acid signaling', 'Mitochondrial function'] },
  { id: 'DC010', drugName: 'Simvastatin', brandNames: ['Zocor'], genericName: 'simvastatin', compositeScore: 64, kgScore: 59, molSimScore: 52, safetyScore: 87, clinicalScore: 58, safetyTier: 'green', mechanism: 'HMG-CoA reductase inhibitor with pleiotropic neuroprotective anti-inflammatory effects', clinicalPhase: 'Phase II', ipStatus: 'Off-Patent', diseaseId: 'D001', targets: ['HMGCR', 'GGPS1'], pathways: ['Cholesterol biosynthesis', 'Isoprenoid pathway'] },
  // Alzheimer's candidates
  { id: 'DC011', drugName: 'Donepezil', brandNames: ['Aricept'], genericName: 'donepezil hydrochloride', compositeScore: 85, kgScore: 90, molSimScore: 83, safetyScore: 88, clinicalScore: 78, safetyTier: 'green', mechanism: 'Acetylcholinesterase inhibitor enhancing cholinergic neurotransmission', clinicalPhase: 'Approved (AD)', ipStatus: 'Off-Patent', diseaseId: 'D002', targets: ['ACHE', 'BCHE'], pathways: ['Cholinergic synapse', 'Acetylcholine metabolism'] },
  { id: 'DC012', drugName: 'Memantine', brandNames: ['Namenda'], genericName: 'memantine hydrochloride', compositeScore: 82, kgScore: 86, molSimScore: 79, safetyScore: 91, clinicalScore: 72, safetyTier: 'green', mechanism: 'NMDA receptor antagonist preventing excitotoxic calcium influx', clinicalPhase: 'Approved (AD)', ipStatus: 'Off-Patent', diseaseId: 'D002', targets: ['GRIN2A', 'GRIN2B'], pathways: ['Glutamatergic synapse', 'Calcium signaling'] },
  { id: 'DC013', drugName: 'Levetiracetam', brandNames: ['Keppra'], genericName: 'levetiracetam', compositeScore: 71, kgScore: 66, molSimScore: 63, safetyScore: 93, clinicalScore: 62, safetyTier: 'green', mechanism: 'SV2A modulator with neuroprotective anti-hyperexcitability effects', clinicalPhase: 'Phase II', ipStatus: 'Off-Patent', diseaseId: 'D002', targets: ['SV2A'], pathways: ['Synaptic vesicle cycling', 'GABAergic signaling'] },
];

// === CLINICAL TRIALS ===
export const clinicalTrials: ClinicalTrial[] = [
  { id: 'CT001', nctId: 'NCT04125737', title: 'Memantine for Huntington\'s Disease Motor Symptoms', phase: 'Phase II', status: 'Recruiting', enrollment: 120, startDate: '2020-03-15', completionDate: '2024-09-30', drugName: 'Memantine', disease: "Huntington's Disease", outcome: 'Ongoing' },
  { id: 'CT002', nctId: 'NCT03821332', title: 'Riluzole in Huntington\'s Disease Pilot Study', phase: 'Phase II', status: 'Completed', enrollment: 37, startDate: '2019-06-01', completionDate: '2022-12-31', drugName: 'Riluzole', disease: "Huntington's Disease", outcome: 'Positive - Significant improvement in motor scores' },
  { id: 'CT003', nctId: 'NCT04567890', title: 'Metformin for Neuroprotection in HD', phase: 'Phase I', status: 'Active, not recruiting', enrollment: 24, startDate: '2021-01-10', completionDate: '2023-06-30', drugName: 'Metformin', disease: "Huntington's Disease", outcome: 'Ongoing' },
  { id: 'CT004', nctId: 'NCT05234567', title: 'CBD for Chorea in Huntington\'s Disease', phase: 'Phase I', status: 'Recruiting', enrollment: 30, startDate: '2022-04-20', completionDate: '2025-03-15', drugName: 'Cannabidiol', disease: "Huntington's Disease", outcome: 'Ongoing' },
  { id: 'CT005', nctId: 'NCT03987654', title: 'Dexamethasone for Neuroinflammation in HD', phase: 'Phase III', status: 'Recruiting', enrollment: 200, startDate: '2021-09-01', completionDate: '2025-12-31', drugName: 'Dexamethasone', disease: "Huntington's Disease", outcome: 'Ongoing' },
  { id: 'CT006', nctId: 'NCT04890123', title: 'Donepezil Cognitive Enhancement in Early AD', phase: 'Phase IV', status: 'Completed', enrollment: 500, startDate: '2018-01-15', completionDate: '2022-06-30', drugName: 'Donepezil', disease: "Alzheimer's Disease", outcome: 'Positive - Cognitive improvement maintained' },
];

// === KNOWLEDGE GRAPH ===
export const graphNodes: GraphNode[] = [
  { id: 'n1', label: 'Memantine', type: 'drug', x: 100, y: 200 },
  { id: 'n2', label: "Huntington's Disease", type: 'disease', x: 700, y: 200 },
  { id: 'n3', label: 'GRIN2A', type: 'gene', x: 300, y: 100 },
  { id: 'n4', label: 'GRIN2B', type: 'gene', x: 300, y: 300 },
  { id: 'n5', label: 'NMDA Receptor', type: 'protein', x: 400, y: 200 },
  { id: 'n6', label: 'Glutamatergic Synapse', type: 'pathway', x: 500, y: 120 },
  { id: 'n7', label: 'Calcium Signaling', type: 'pathway', x: 500, y: 280 },
  { id: 'n8', label: 'HTT Gene', type: 'gene', x: 600, y: 350 },
  { id: 'n9', label: 'Excitotoxicity', type: 'pathway', x: 550, y: 200 },
  { id: 'n10', label: 'Riluzole', type: 'drug', x: 100, y: 400 },
  { id: 'n11', label: 'SLC1A2', type: 'gene', x: 250, y: 450 },
  { id: 'n12', label: 'EAAT2', type: 'protein', x: 400, y: 400 },
];

export const graphEdges: GraphEdge[] = [
  { source: 'n1', target: 'n5', relation: 'inhibits', evidence: 0.95 },
  { source: 'n1', target: 'n3', relation: 'interacts_with', evidence: 0.92 },
  { source: 'n1', target: 'n4', relation: 'interacts_with', evidence: 0.88 },
  { source: 'n5', target: 'n6', relation: 'participates_in', evidence: 0.97 },
  { source: 'n5', target: 'n7', relation: 'participates_in', evidence: 0.91 },
  { source: 'n6', target: 'n2', relation: 'associated_with', evidence: 0.85 },
  { source: 'n7', target: 'n9', relation: 'causes_side_effect', evidence: 0.78 },
  { source: 'n9', target: 'n2', relation: 'associated_with', evidence: 0.89 },
  { source: 'n8', target: 'n2', relation: 'associated_with', evidence: 0.99 },
  { source: 'n1', target: 'n2', relation: 'treats', evidence: 0.87 },
  { source: 'n10', target: 'n11', relation: 'interacts_with', evidence: 0.84 },
  { source: 'n11', target: 'n12', relation: 'expressed_in', evidence: 0.90 },
  { source: 'n12', target: 'n6', relation: 'participates_in', evidence: 0.86 },
  { source: 'n10', target: 'n2', relation: 'treats', evidence: 0.84 },
];

// === USERS ===
export const users: User[] = [
  { id: 'U001', name: 'Dr. Sarah Chen', email: 'schen@pharma.com', role: 'Principal Investigator', department: 'Oncology', status: 'active', lastActive: '2 hours ago' },
  { id: 'U002', name: 'James Miller', email: 'j.miller@biotech.io', role: 'Data Scientist', department: 'ML Engineering', status: 'active', lastActive: '30 min ago' },
  { id: 'U003', name: 'Dr. Priya Sharma', email: 'psharma@university.edu', role: 'Researcher', department: 'Neuroscience', status: 'active', lastActive: '1 hour ago' },
  { id: 'U004', name: 'Alex Thompson', email: 'athompson@pharma.com', role: 'Admin', department: 'IT', status: 'active', lastActive: '15 min ago' },
  { id: 'U005', name: 'Dr. Maria Garcia', email: 'mgarcia@cro.com', role: 'Project Lead', department: 'Drug Discovery', status: 'active', lastActive: '4 hours ago' },
  { id: 'U006', name: 'Robert Kim', email: 'rkim@startup.co', role: 'CTO', department: 'Engineering', status: 'invited', lastActive: 'Never' },
  { id: 'U007', name: 'Dr. Lisa Wang', email: 'lwang@university.edu', role: 'Researcher', department: 'Genomics', status: 'active', lastActive: '3 hours ago' },
  { id: 'U008', name: 'Tom Anderson', email: 'tanderson@pharma.com', role: 'Business Dev', department: 'Partnerships', status: 'suspended', lastActive: '2 weeks ago' },
];

// === NOTIFICATIONS ===
export const notifications: Notification[] = [
  { id: 'N001', title: 'New prediction validated', message: 'Memantine for Huntington\'s has been confirmed by wet-lab results', type: 'success', time: '5 min ago', read: false },
  { id: 'N002', title: 'Usage limit warning', message: 'You have used 80% of your daily query limit', type: 'warning', time: '1 hour ago', read: false },
  { id: 'N003', title: 'Report ready', message: 'Your Huntington\'s Disease analysis report is ready for download', type: 'info', time: '2 hours ago', read: true },
  { id: 'N004', title: 'Team member joined', message: 'Dr. Priya Sharma has joined your project workspace', type: 'info', time: '3 hours ago', read: true },
  { id: 'N005', title: 'Safety alert', message: 'New contraindication detected for Lithium Carbonate + HD population', type: 'error', time: '5 hours ago', read: false },
];

// === AUDIT LOG ===
export const auditLogs: AuditLogEntry[] = [
  { id: 'AL001', action: 'QUERY_EXECUTED', user: 'Dr. Sarah Chen', resource: 'Disease Search: Huntington\'s', timestamp: '2026-06-10 09:15:32', ip: '192.168.1.45', details: 'Searched for Huntington\'s Disease candidates' },
  { id: 'AL002', action: 'REPORT_DOWNLOADED', user: 'James Miller', resource: 'Report #R-2026-0456', timestamp: '2026-06-10 08:45:12', ip: '10.0.0.23', details: 'Downloaded PDF report for Alzheimer\'s candidates' },
  { id: 'AL003', action: 'USER_INVITED', user: 'Alex Thompson', resource: 'rkim@startup.co', timestamp: '2026-06-10 08:30:00', ip: '192.168.1.10', details: 'Invited Robert Kim as CTO' },
  { id: 'AL004', action: 'PHI_ACCESSED', user: 'Dr. Maria Garcia', resource: 'Patient Dataset #PD-2026-789', timestamp: '2026-06-10 07:15:45', ip: '10.0.1.56', details: 'Accessed PHI records for CRO project' },
  { id: 'AL005', action: 'API_KEY_GENERATED', user: 'James Miller', resource: 'API Key: dk_prod_***xyz', timestamp: '2026-06-09 16:22:10', ip: '10.0.0.23', details: 'Generated production API key' },
  { id: 'AL006', action: 'ROLE_CHANGED', user: 'Alex Thompson', resource: 'Tom Anderson', timestamp: '2026-06-09 14:00:00', ip: '192.168.1.10', details: 'Changed role from User to Suspended' },
];

// === BILLING ===
export const subscriptionPlans = [
  { id: 'free', name: 'Free Academic', price: '$0', period: 'forever', queries: 10, api: false, features: ['Basic disease search', 'Top 3 candidates', 'PDF reports', 'Community support'], users: 'Unlimited academics' },
  { id: 'starter', name: 'Starter', price: '$499', period: '/month', queries: 100, api: true, features: ['Full candidate list', 'Safety profiles', 'API access (1K calls/day)', 'Email support', 'Team collaboration (5 seats)'], users: 'Up to 5' },
  { id: 'professional', name: 'Professional', price: '$5,000', period: '/month', queries: 1000, api: true, features: ['Unlimited queries', 'Full knowledge graph', 'Advanced safety', 'API access (50K calls/day)', 'SSO/SAML', 'GxP mode', 'Priority support', 'Custom reports'], users: 'Up to 25' },
  { id: 'enterprise', name: 'Enterprise', price: 'Custom', period: '', queries: -1, api: true, features: ['Everything in Professional', 'Unlimited API', 'Dedicated VPC', 'Custom ML models', 'White-label', 'SCIM provisioning', 'FedRAMP', 'Dedicated CSM', 'SLA guarantee'], users: 'Unlimited' },
  { id: 'discovery', name: 'Discovery Deal', price: '$50M-$500M', period: 'per deal', queries: -1, api: true, features: ['Exclusive rights to validated lead', 'Full evidence package', 'Mechanistic proof', 'IP licensing', 'Regulatory support', 'Due diligence portal'], users: 'N/A' },
];

export const billingHistory = [
  { id: 'INV-001', date: '2026-06-01', amount: '$5,000.00', status: 'Paid', description: 'Professional Plan - June 2026' },
  { id: 'INV-002', date: '2026-05-01', amount: '$5,000.00', status: 'Paid', description: 'Professional Plan - May 2026' },
  { id: 'INV-003', date: '2026-04-01', amount: '$5,000.00', status: 'Paid', description: 'Professional Plan - April 2026' },
  { id: 'INV-004', date: '2026-03-01', amount: '$499.00', status: 'Paid', description: 'Starter Plan - March 2026' },
  { id: 'INV-005', date: '2026-06-05', amount: '$342.50', status: 'Pending', description: 'API Overage - 34,250 additional calls' },
];

// === API KEYS ===
export const apiKeys = [
  { id: 'AK001', name: 'Production Key', key: 'dk_live_*********************xyz', created: '2026-01-15', lastUsed: '5 min ago', status: 'active', calls: 45230 },
  { id: 'AK002', name: 'Staging Key', key: 'dk_test_*********************abc', created: '2026-03-20', lastUsed: '2 days ago', status: 'active', calls: 12450 },
  { id: 'AK003', name: 'Research Key', key: 'dk_test_*********************def', created: '2026-05-01', lastUsed: '1 week ago', status: 'inactive', calls: 3280 },
];

// === WEBHOOKS ===
export const webhooks = [
  { id: 'WH001', url: 'https://api.myapp.com/webhooks/drugos', events: ['query.completed', 'report.ready'], status: 'active', lastTriggered: '1 hour ago', successRate: 99.2 },
  { id: 'WH002', url: 'https://slack.bot.com/hooks/notifications', events: ['safety.alert', 'usage.warning'], status: 'active', lastTriggered: '5 hours ago', successRate: 100 },
  { id: 'WH003', url: 'https://internal.company.com/api/update', events: ['candidate.updated'], status: 'inactive', lastTriggered: '2 weeks ago', successRate: 85.7 },
];

// === USAGE METRICS ===
export const usageMetrics = {
  queries: { used: 342, limit: 1000, period: 'June 2026' },
  apiCalls: { used: 45230, limit: 50000, period: 'June 2026' },
  computeHours: { used: 128, limit: 500, period: 'June 2026' },
  storage: { used: 12.4, limit: 50, period: 'GB' },
  reports: { used: 8, limit: 50, period: 'June 2026' },
  seats: { used: 18, limit: 25, period: 'Current' },
};

// === DATA SOURCES ===
export const dataSources = [
  { id: 'DS001', name: 'DrugBank', type: 'Drug Database', records: 13570, lastSync: '2026-06-01', status: 'healthy', coverage: 99.2 },
  { id: 'DS002', name: 'ChEMBL', type: 'Bioactivity', records: 2150000, lastSync: '2026-05-28', status: 'healthy', coverage: 97.8 },
  { id: 'DS003', name: 'OpenTargets', type: 'Target-Disease', records: 482000, lastSync: '2026-06-02', status: 'healthy', coverage: 95.5 },
  { id: 'DS004', name: 'ClinicalTrials.gov', type: 'Clinical Trials', records: 425000, lastSync: '2026-06-03', status: 'healthy', coverage: 98.1 },
  { id: 'DS005', name: 'UniProt', type: 'Protein', records: 570000, lastSync: '2026-05-30', status: 'degraded', coverage: 92.3 },
  { id: 'DS006', name: 'STRING', type: 'Protein Interactions', records: 12500000, lastSync: '2026-05-25', status: 'healthy', coverage: 96.7 },
  { id: 'DS007', name: 'GEO', type: 'Gene Expression', records: 4100000, lastSync: '2026-05-20', status: 'healthy', coverage: 88.9 },
  { id: 'DS008', name: 'SIDER', type: 'Side Effects', records: 145000, lastSync: '2026-05-15', status: 'healthy', coverage: 91.4 },
  { id: 'DS009', name: 'STITCH', type: 'Chemical-Protein', records: 960000, lastSync: '2026-05-18', status: 'degraded', coverage: 89.7 },
  { id: 'DS010', name: 'DRKG', type: 'Knowledge Graph', records: 5870000, lastSync: '2026-06-01', status: 'healthy', coverage: 94.2 },
];

// === TRENDING DISEASES ===
export const trendingDiseases = [
  { name: "Huntington's Disease", queries: 1247, change: +23 },
  { name: "Alzheimer's Disease", queries: 3421, change: +12 },
  { name: 'Glioblastoma', queries: 892, change: +45 },
  { name: 'ALS', queries: 734, change: +18 },
  { name: 'Pancreatic Cancer', queries: 1563, change: +8 },
  { name: 'Sickle Cell Disease', queries: 456, change: +31 },
];

// === RECENT QUERIES ===
export const recentQueries = [
  { id: 'Q001', disease: "Huntington's Disease", date: '2026-06-10 09:15', candidates: 10, topScore: 87 },
  { id: 'Q002', disease: "Alzheimer's Disease", date: '2026-06-09 16:30', candidates: 15, topScore: 85 },
  { id: 'Q003', disease: 'Pancreatic Cancer', date: '2026-06-09 11:45', candidates: 8, topScore: 79 },
  { id: 'Q004', disease: 'ALS', date: '2026-06-08 14:20', candidates: 12, topScore: 76 },
  { id: 'Q005', disease: 'Cystic Fibrosis', date: '2026-06-08 09:00', candidates: 6, topScore: 72 },
];

// === TEAM MEMBERS ===
export const teamMembers = users.slice(0, 6);

// === PROJECTS ===
export const projects = [
  { id: 'P001', name: 'HD Repurposing Study', disease: "Huntington's Disease", members: 4, candidates: 10, status: 'Active', progress: 65 },
  { id: 'P002', name: 'AD Drug Discovery', disease: "Alzheimer's Disease", members: 6, candidates: 15, status: 'Active', progress: 40 },
  { id: 'P003', name: 'Rare Disease Initiative', disease: 'Multiple conditions', members: 3, candidates: 22, status: 'Planning', progress: 15 },
  { id: 'P004', name: 'Oncology Pipeline', disease: 'Pancreatic Cancer', members: 5, candidates: 8, status: 'Active', progress: 80 },
];

// === DEAL PIPELINE (for BILL/INV screens) ===
export const dealPipeline = [
  { id: 'DP001', company: 'Pfizer', disease: "Huntington's Disease", stage: 'Due Diligence', value: '$250M', probability: 45, lead: 'Aseem' },
  { id: 'DP002', company: 'Novartis', disease: "Alzheimer's Disease", stage: 'Negotiation', value: '$180M', probability: 60, lead: 'Aseem' },
  { id: 'DP003', company: 'Roche', disease: 'ALS', stage: 'Initial Contact', value: '$120M', probability: 20, lead: 'Manoj' },
  { id: 'DP004', company: 'AstraZeneca', disease: 'Glioblastoma', stage: 'LOI Signed', value: '$350M', probability: 35, lead: 'Aseem' },
  { id: 'DP005', company: 'Biogen', disease: "Parkinson's Disease", stage: 'Term Sheet', value: '$200M', probability: 55, lead: 'Manoj' },
];

// === ORGANIZATION ===
export const organization = {
  name: 'DrugOS Corp',
  plan: 'Professional',
  seats: { used: 18, total: 25 },
  domains: ['drugos.io', 'pharma.com'],
  ssoEnabled: true,
  ssoProvider: 'Okta',
  mfaRequired: true,
  dataResidency: 'US-East',
  gxpMode: false,
  founded: '2026',
  team: 'Team Cosmic',
};

// === FEATURE FLAGS ===
export const featureFlags = [
  { id: 'FF001', name: 'GxP Validated Mode', enabled: false, description: 'Enable 21 CFR Part 11 compliance features', rollout: 0 },
  { id: 'FF002', name: 'Custom ML Models', enabled: true, description: 'Allow enterprise users to deploy custom models', rollout: 100 },
  { id: 'FF003', name: 'White-Label Reports', enabled: true, description: 'Remove DrugOS branding from reports', rollout: 100 },
  { id: 'FF004', name: 'Batch Query API', enabled: true, description: 'Support batch disease queries via API', rollout: 75 },
  { id: 'FF005', name: 'Real-time Collaboration', enabled: false, description: 'Live cursor and editing collaboration', rollout: 0 },
  { id: 'FF006', name: 'FedRAMP Module', enabled: false, description: 'Government compliance module', rollout: 0 },
  { id: 'FF007', name: 'Multi-Omics Fusion', enabled: false, description: 'Genomics + Proteomics + Metabolomics', rollout: 10 },
];

// === STATUS PAGE ===
export const systemStatus = [
  { service: 'API Gateway', status: 'operational', uptime: 99.99, latency: '45ms' },
  { service: 'Query Engine', status: 'operational', uptime: 99.95, latency: '12s' },
  { service: 'Knowledge Graph', status: 'operational', uptime: 99.98, latency: '230ms' },
  { service: 'Model Inference', status: 'operational', uptime: 99.92, latency: '8.5s' },
  { service: 'Report Generator', status: 'degraded', uptime: 99.80, latency: '25s' },
  { service: 'Authentication', status: 'operational', uptime: 99.99, latency: '120ms' },
];

// === Saved queries ===
export const savedQueries = [
  { id: 'SQ001', name: 'HD Top Candidates', disease: "Huntington's Disease", filters: 'Safety: Green | Score > 70', created: '2026-06-08', results: 6 },
  { id: 'SQ002', name: 'AD Approved Drugs', disease: "Alzheimer's Disease", filters: 'Phase: Approved | Safety: Green', created: '2026-06-05', results: 3 },
  { id: 'SQ003', name: 'Oncology High-Score', disease: 'Pancreatic Cancer', filters: 'Score > 75 | IP: Novel', created: '2026-06-01', results: 4 },
];

// === Blog posts ===
export const blogPosts = [
  { id: 'BP001', title: 'DrugOS Identifies 3 Novel Candidates for Huntington\'s Disease', date: '2026-06-08', category: 'Research', excerpt: 'Our platform has identified three promising drug repurposing candidates validated against published literature...' },
  { id: 'BP002', title: 'The Data Flywheel: How DrugOS Gets Smarter Every Day', date: '2026-06-01', category: 'Technology', excerpt: 'Every validated prediction becomes proprietary training signal. Here\'s how the compounding moat works...' },
  { id: 'BP003', title: 'Partnering with Rare Disease Foundations', date: '2026-05-25', category: 'Partnerships', excerpt: 'We are excited to announce partnerships with three rare disease foundations to accelerate treatment discovery...' },
];

// === Careers ===
export const careers = [
  { id: 'CR001', title: 'Senior ML Engineer - Graph Neural Networks', location: 'Remote / San Francisco', type: 'Full-time', department: 'Engineering' },
  { id: 'CR002', title: 'Biomedical Data Engineer', location: 'Remote', type: 'Full-time', department: 'Data' },
  { id: 'CR003', title: 'Business Development Manager - Pharma', location: 'Boston / NYC', type: 'Full-time', department: 'Business' },
  { id: 'CR004', title: 'Product Designer - Life Sciences', location: 'Remote', type: 'Full-time', department: 'Design' },
];

// === PATENT DATA ===
export interface Patent {
  id: string;
  title: string;
  patentNumber: string;
  status: 'active' | 'expired' | 'pending' | 'abandoned';
  jurisdiction: string;
  assignee: string;
  filingDate: string;
  expirationDate: string;
  claims: number;
  drugName: string;
}

export const patents: Patent[] = [
  { id: 'PT001', title: 'Use of Memantine for Treating Huntington Disease', patentNumber: 'US-10,123,456', status: 'expired', jurisdiction: 'US', assignee: 'Forest Laboratories', filingDate: '2005-03-15', expirationDate: '2023-03-15', claims: 12, drugName: 'Memantine' },
  { id: 'PT002', title: 'Riluzole Compositions for Neurodegenerative Disorders', patentNumber: 'US-9,876,543', status: 'expired', jurisdiction: 'US', assignee: 'Sanofi', filingDate: '2002-07-20', expirationDate: '2021-07-20', claims: 8, drugName: 'Riluzole' },
  { id: 'PT003', title: 'Dexamethasone for Neuroinflammation Treatment', patentNumber: 'US-11,234,567', status: 'pending', jurisdiction: 'US/EU', assignee: 'Merck', filingDate: '2023-01-10', expirationDate: '2043-01-10', claims: 15, drugName: 'Dexamethasone' },
  { id: 'PT004', title: 'Cannabidiol Formulations for Movement Disorders', patentNumber: 'US-11,567,890', status: 'active', jurisdiction: 'US/EU/JP', assignee: 'GW Pharmaceuticals', filingDate: '2020-06-15', expirationDate: '2040-06-15', claims: 22, drugName: 'Cannabidiol' },
  { id: 'PT005', title: 'Fingolimod for Neuroprotection', patentNumber: 'US-10,987,654', status: 'active', jurisdiction: 'US', assignee: 'Novartis', filingDate: '2015-09-01', expirationDate: '2035-09-01', claims: 18, drugName: 'Fingolimod' },
  { id: 'PT006', title: 'Donepezil for Cognitive Enhancement', patentNumber: 'US-8,765,432', status: 'expired', jurisdiction: 'US', assignee: 'Eisai', filingDate: '1998-11-20', expirationDate: '2019-11-20', claims: 10, drugName: 'Donepezil' },
  { id: 'PT007', title: 'Metformin for Autophagy Induction', patentNumber: 'WO-2024/123456', status: 'pending', jurisdiction: 'PCT', assignee: 'University Research', filingDate: '2024-02-01', expirationDate: '2044-02-01', claims: 14, drugName: 'Metformin' },
];

// === EVIDENCE ITEMS ===
export interface EvidenceItem {
  id: string;
  title: string;
  source: string;
  type: 'clinical' | 'preclinical' | 'computational' | 'literature' | 'patent';
  quality: number;
  drugName: string;
  disease: string;
  year: number;
  summary: string;
}

export const evidenceItems: EvidenceItem[] = [
  { id: 'EV001', title: 'Memantine reduces excitotoxicity in HD models', source: 'Nature Neuroscience', type: 'preclinical', quality: 92, drugName: 'Memantine', disease: "Huntington's Disease", year: 2019, summary: 'In vitro study showing memantine reduces NMDA-mediated excitotoxicity in striatal neurons derived from HD patient iPSCs.' },
  { id: 'EV002', title: 'Riluzole improves motor scores in HD patients', source: 'Lancet Neurology', type: 'clinical', quality: 88, drugName: 'Riluzole', disease: "Huntington's Disease", year: 2022, summary: 'Phase II trial demonstrating significant improvement in UHDRS motor scores with riluzole treatment.' },
  { id: 'EV003', title: 'KG prediction: Memantine-HD association', source: 'DrugOS Knowledge Graph', type: 'computational', quality: 91, drugName: 'Memantine', disease: "Huntington's Disease", year: 2026, summary: 'Graph Transformer model predicts strong drug-disease association based on shared pathway and target analysis.' },
  { id: 'EV004', title: 'Molecular similarity to known HD therapeutics', source: 'ChEMBL', type: 'computational', quality: 82, drugName: 'Memantine', disease: "Huntington's Disease", year: 2026, summary: 'Structural similarity analysis shows 0.78 Tanimoto coefficient with reference HD compounds.' },
  { id: 'EV005', title: 'Metformin promotes autophagy in HD neurons', source: 'Cell Reports', type: 'preclinical', quality: 85, drugName: 'Metformin', disease: "Huntington's Disease", year: 2021, summary: 'AMPK-dependent autophagy induction reduces mutant huntingtin aggregates in cellular and mouse models.' },
  { id: 'EV006', title: 'CBD reduces chorea severity in HD', source: 'JAMA Neurology', type: 'clinical', quality: 76, drugName: 'Cannabidiol', disease: "Huntington's Disease", year: 2024, summary: 'Open-label study of CBD showing reduction in chorea severity with good tolerability profile.' },
  { id: 'EV007', title: 'Donepezil enhances cognition in early AD', source: 'NEJM', type: 'clinical', quality: 95, drugName: 'Donepezil', disease: "Alzheimer's Disease", year: 2018, summary: 'Phase IV trial confirming sustained cognitive benefit over 24-month follow-up period.' },
  { id: 'EV008', title: 'Dexamethasone neuroinflammation pilot', source: 'Annals of Neurology', type: 'clinical', quality: 79, drugName: 'Dexamethasone', disease: "Huntington's Disease", year: 2023, summary: 'Pilot study showing reduction in neuroinflammatory markers in CSF of HD patients.' },
];

// === ADMET DATA ===
export interface ADMETProfile {
  drugName: string;
  absorption: number;
  distribution: number;
  metabolism: number;
  excretion: number;
  toxicity: number;
}

export const admetProfiles: ADMETProfile[] = [
  { drugName: 'Memantine', absorption: 85, distribution: 78, metabolism: 82, excretion: 88, toxicity: 92 },
  { drugName: 'Riluzole', absorption: 72, distribution: 68, metabolism: 65, excretion: 75, toxicity: 85 },
  { drugName: 'Dexamethasone', absorption: 90, distribution: 82, metabolism: 58, excretion: 70, toxicity: 62 },
  { drugName: 'Metformin', absorption: 78, distribution: 65, metabolism: 92, excretion: 88, toxicity: 95 },
  { drugName: 'Cannabidiol', absorption: 55, distribution: 85, metabolism: 48, excretion: 72, toxicity: 80 },
  { drugName: 'Fingolimod', absorption: 92, distribution: 88, metabolism: 52, excretion: 68, toxicity: 58 },
  { drugName: 'Donepezil', absorption: 88, distribution: 82, metabolism: 60, excretion: 78, toxicity: 82 },
  { drugName: 'Lithium Carbonate', absorption: 80, distribution: 72, metabolism: 45, excretion: 55, toxicity: 52 },
];

// === OFF-TARGET PREDICTIONS ===
export interface OffTargetPrediction {
  drugName: string;
  target: string;
  probability: number;
  severity: 'low' | 'medium' | 'high';
  organSystem: string;
}

export const offTargetPredictions: OffTargetPrediction[] = [
  { drugName: 'Memantine', target: '5-HT3 receptor', probability: 0.35, severity: 'low', organSystem: 'Nervous' },
  { drugName: 'Memantine', target: 'Dopamine D2', probability: 0.22, severity: 'low', organSystem: 'Nervous' },
  { drugName: 'Riluzole', target: 'Na+ channels', probability: 0.45, severity: 'medium', organSystem: 'Cardiac' },
  { drugName: 'Dexamethasone', target: 'Mineralocorticoid receptor', probability: 0.68, severity: 'high', organSystem: 'Endocrine' },
  { drugName: 'Dexamethasone', target: 'Glucose metabolism', probability: 0.72, severity: 'high', organSystem: 'Metabolic' },
  { drugName: 'Lithium Carbonate', target: 'Thyroid peroxidase', probability: 0.58, severity: 'medium', organSystem: 'Endocrine' },
  { drugName: 'Fingolimod', target: 'Cardiac S1P receptors', probability: 0.55, severity: 'high', organSystem: 'Cardiac' },
  { drugName: 'Donepezil', target: 'Peripheral AChE', probability: 0.42, severity: 'medium', organSystem: 'GI' },
];

// === DRUG INTERACTIONS ===
export interface DrugInteraction {
  drug1: string;
  drug2: string;
  severity: 'minor' | 'moderate' | 'major' | 'contraindicated';
  description: string;
  mechanism: string;
}

export const drugInteractions: DrugInteraction[] = [
  { drug1: 'Memantine', drug2: 'Amantadine', severity: 'moderate', description: 'Additive CNS effects possible', mechanism: 'Both are NMDA antagonists' },
  { drug1: 'Riluzole', drug2: 'CYP1A2 inhibitors', severity: 'major', description: 'Increased riluzole exposure', mechanism: 'CYP1A2 metabolism inhibition' },
  { drug1: 'Dexamethasone', drug2: 'NSAIDs', severity: 'moderate', description: 'Increased GI ulcer risk', mechanism: 'Additive GI mucosal damage' },
  { drug1: 'Metformin', drug2: 'Contrast dyes', severity: 'contraindicated', description: 'Risk of lactic acidosis', mechanism: 'Impaired renal function' },
  { drug1: 'Fingolimod', drug2: 'Beta-blockers', severity: 'major', description: 'Additive bradycardia risk', mechanism: 'Combined heart rate effects' },
  { drug1: 'Lithium Carbonate', drug2: 'NSAIDs', severity: 'major', description: 'Increased lithium levels', mechanism: 'Reduced renal clearance' },
  { drug1: 'Donepezil', drug2: 'Anticholinergics', severity: 'contraindicated', description: 'Pharmacological antagonism', mechanism: 'Opposing cholinergic effects' },
];

// === TYPE EXPORTS ===
export type SafetyTier = DrugCandidate['safetyTier'];
