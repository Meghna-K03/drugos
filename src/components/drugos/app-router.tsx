'use client'

import React, { useState, createContext, useContext, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Search, Shield, Database, Users, CreditCard, Settings, HelpCircle, ChevronDown, ChevronRight,
  Bell, Menu, X, TrendingUp, Scale, Code, Globe, Lock, LayoutDashboard, FileQuestion, Plus,
  Download, Star, Check, AlertTriangle, Activity, Eye, Filter, ArrowRight, ArrowLeft,
  ExternalLink, Zap, BarChart3, GitBranch, FileText, FolderOpen, Key, Globe2, BookOpen,
  Heart, Briefcase, Mail, MapPin, Phone, Clock, Calendar, Tag, Layers, Cpu, GitCommit,
  RefreshCw, AlertCircle, CheckCircle2, XCircle, Info, ChevronUp, MoreHorizontal, PlusCircle,
  Minus, Edit, Trash2, Share2, Bookmark, Copy, Users as UsersIcon, LogOut, User, Building,
  GraduationCap, FlaskConical, Network, Target, LineChart as LineChartIcon, PieChart as PieChartIcon,
  ArrowUpRight, Play, MessageSquare, Award, ShieldCheck, Server, Cloud, MonitorSmartphone,
  FileCheck, Handshake, Microscope, Beaker, Atom, GitFork, Columns3, FolderKanban
} from 'lucide-react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, LineChart, Line, AreaChart, Area, RadarChart, Radar,
  PolarGrid, PolarAngleAxis, PolarRadiusAxis
} from 'recharts'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Slider } from '@/components/ui/slider'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Progress } from '@/components/ui/progress'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import {
  Breadcrumb, BreadcrumbItem, BreadcrumbLink, BreadcrumbList,
  BreadcrumbPage, BreadcrumbSeparator
} from '@/components/ui/breadcrumb'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger
} from '@/components/ui/dialog'
import {
  Sheet, SheetContent, SheetTrigger, SheetTitle
} from '@/components/ui/sheet'
import {
  diseases, drugCandidates, clinicalTrials, graphNodes, graphEdges, users,
  notifications as notifData, auditLogs, subscriptionPlans, billingHistory, apiKeys,
  webhooks, usageMetrics, dataSources, trendingDiseases, recentQueries, projects,
  dealPipeline, organization, featureFlags, systemStatus, savedQueries, blogPosts, careers
} from '@/lib/mock-data'
import { cn } from '@/lib/utils'
import { coreScreens } from './core-screens'
import { allScreens } from './all-screens'
import { DrugOSNavContext } from './nav-context'
import { AuthProvider, useAuth } from './auth-context'
import { useSystemStatus } from './data-hooks'
import { api } from '@/lib/api-client'

// =====================================================================
// TYPES & ROUTER CONTEXT
// =====================================================================

type Route =
  | { page: 'landing' }
  | { page: 'pricing' }
  | { page: 'features'; slug: string }
  | { page: 'about' }
  | { page: 'security' }
  | { page: 'status' }
  | { page: 'blog' }
  | { page: 'contact' }
  | { page: 'careers' }
  | { page: 'case-studies' }
  | { page: 'login' }
  | { page: 'register' }
  | { page: 'forgot-password' }
  | { page: 'reset-password' }
  | { page: 'mfa-challenge' }
  | { page: 'email-verification' }
  | { page: 'academic-verification' }
  | { page: 'org-selection' }
  | { page: 'onboarding-welcome' }
  | { page: 'onboarding-role' }
  | { page: 'onboarding-workspace' }
  | { page: 'onboarding-invite' }
  | { page: 'admin-approval' }
  | { page: 'account-locked' }
  | { page: 'app'; section: string; sub?: string; id?: string }

interface RouterContextType {
  route: Route
  navigate: (r: Route) => void
}

const RouterContext = createContext<RouterContextType>({
  route: { page: 'landing' },
  navigate: () => {},
})

function useRouter() {
  return useContext(RouterContext)
}

// =====================================================================
// COLOR CONSTANTS
// =====================================================================

const COLORS = {
  primary: '#5B4FCF',
  primaryLight: '#7B6FEF',
  accentGreen: '#1D9E75',
  accentOrange: '#D4853A',
  accentRed: '#C0392B',
  bg: '#F8F8FA',
}

const CHART_COLORS = ['#5B4FCF', '#1D9E75', '#D4853A', '#C0392B', '#8B5CF6', '#06B6D4']

// =====================================================================
// SHARED UI COMPONENTS
// =====================================================================

function DrugOSLogo({ size = 'md' }: { size?: 'sm' | 'md' | 'lg' }) {
  const s = size === 'sm' ? 'w-7 h-7 text-sm' : size === 'lg' ? 'w-12 h-12 text-xl' : 'w-9 h-9 text-lg'
  return (
    <div className="flex items-center gap-2.5">
      <div className={`${s} rounded-xl bg-gradient-to-br from-[#5B4FCF] to-[#7B6FEF] flex items-center justify-center text-white font-bold shadow-lg shadow-[#5B4FCF]/20`}>
        D
      </div>
      {size !== 'sm' && (
        <span className={`font-bold text-foreground ${size === 'lg' ? 'text-2xl' : 'text-lg'}`}>
          DrugOS
        </span>
      )}
    </div>
  )
}

function ScoreBar({ score, size = 'md' }: { score: number; size?: 'sm' | 'md' | 'lg' }) {
  const color = score >= 80 ? 'bg-emerald-500' : score >= 60 ? 'bg-amber-500' : 'bg-red-500'
  const h = size === 'sm' ? 'h-1.5' : size === 'lg' ? 'h-3' : 'h-2'
  return (
    <div className="w-full bg-slate-100 rounded-full overflow-hidden">
      <div className={`${color} ${h} rounded-full transition-all`} style={{ width: `${score}%` }} />
    </div>
  )
}

function SafetyBadge({ tier }: { tier: 'green' | 'yellow' | 'red' }) {
  const c = { green: 'bg-emerald-50 text-emerald-700 border-emerald-200', yellow: 'bg-amber-50 text-amber-700 border-amber-200', red: 'bg-red-50 text-red-700 border-red-200' }
  const l = { green: 'Safe', yellow: 'Caution', red: 'Risk' }
  return <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold border ${c[tier]}`}>{l[tier]}</span>
}

function StatusDot({ status }: { status: string }) {
  const c = status === 'operational' || status === 'active' || status === 'healthy' || status === 'Paid' ? 'bg-emerald-500' : status === 'degraded' || status === 'yellow' ? 'bg-amber-500' : 'bg-red-500'
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${c}`} />
}

function SectionHeading({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="flex items-end justify-between mb-6">
      <div>
        <h2 className="text-2xl font-bold text-foreground">{title}</h2>
        {subtitle && <p className="text-muted-foreground mt-1">{subtitle}</p>}
      </div>
      {action}
    </div>
  )
}

// =====================================================================
// PUBLIC HEADER & FOOTER
// =====================================================================

function PublicHeader() {
  const { navigate, route } = useRouter()
  const [mobileOpen, setMobileOpen] = useState(false)

  const navItems = [
    { label: 'Features', action: () => navigate({ page: 'features', slug: 'disease-search' }) },
    { label: 'Pricing', action: () => navigate({ page: 'pricing' }) },
    { label: 'About', action: () => navigate({ page: 'about' }) },
    { label: 'Security', action: () => navigate({ page: 'security' }) },
    { label: 'Blog', action: () => navigate({ page: 'blog' }) },
    { label: 'Careers', action: () => navigate({ page: 'careers' }) },
  ]

  return (
    <header className="sticky top-0 z-50 bg-white/80 backdrop-blur-xl border-b border-border/50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          <button onClick={() => navigate({ page: 'landing' })} className="flex items-center gap-2.5 hover:opacity-80 transition-opacity">
            <DrugOSLogo size="sm" />
            <span className="font-bold text-lg text-foreground">DrugOS</span>
          </button>

          <nav className="hidden md:flex items-center gap-1">
            {navItems.map(item => (
              <button key={item.label} onClick={item.action} className="px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors rounded-lg hover:bg-accent">
                {item.label}
              </button>
            ))}
          </nav>

          <div className="hidden md:flex items-center gap-3">
            <Button variant="ghost" size="sm" onClick={() => navigate({ page: 'login' })}>Sign In</Button>
            <Button size="sm" onClick={() => navigate({ page: 'register' })} className="bg-[#5B4FCF] hover:bg-[#4B3FBF]">Start Free</Button>
          </div>

          <button className="md:hidden p-2" onClick={() => setMobileOpen(!mobileOpen)}>
            {mobileOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
          </button>
        </div>
      </div>

      <AnimatePresence>
        {mobileOpen && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="md:hidden border-t border-border bg-white"
          >
            <div className="px-4 py-4 space-y-1">
              {navItems.map(item => (
                <button key={item.label} onClick={() => { item.action(); setMobileOpen(false) }} className="block w-full text-left px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg">
                  {item.label}
                </button>
              ))}
              <Separator className="my-2" />
              <Button variant="ghost" size="sm" className="w-full justify-start" onClick={() => { navigate({ page: 'login' }); setMobileOpen(false) }}>Sign In</Button>
              <Button size="sm" className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => { navigate({ page: 'register' }); setMobileOpen(false) }}>Start Free</Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  )
}

function PublicFooter() {
  const { navigate } = useRouter()
  const footerSections = [
    {
      title: 'Product',
      links: [
        { label: 'Disease Search', action: () => navigate({ page: 'features', slug: 'disease-search' }) },
        { label: 'Knowledge Graph', action: () => navigate({ page: 'features', slug: 'knowledge-graph' }) },
        { label: 'Safety Profiling', action: () => navigate({ page: 'features', slug: 'safety-profiling' }) },
        { label: 'Evidence Reports', action: () => navigate({ page: 'features', slug: 'evidence-reports' }) },
        { label: 'API & Dev Tools', action: () => navigate({ page: 'features', slug: 'api-developer' }) },
        { label: 'Pricing', action: () => navigate({ page: 'pricing' }) },
      ]
    },
    {
      title: 'Company',
      links: [
        { label: 'About', action: () => navigate({ page: 'about' }) },
        { label: 'Blog', action: () => navigate({ page: 'blog' }) },
        { label: 'Careers', action: () => navigate({ page: 'careers' }) },
        { label: 'Case Studies', action: () => navigate({ page: 'case-studies' }) },
        { label: 'Contact', action: () => navigate({ page: 'contact' }) },
      ]
    },
    {
      title: 'Trust',
      links: [
        { label: 'Security', action: () => navigate({ page: 'security' }) },
        { label: 'Status', action: () => navigate({ page: 'status' }) },
        { label: 'Privacy', action: () => navigate({ page: 'landing' }) },
        { label: 'Terms', action: () => navigate({ page: 'landing' }) },
        { label: 'HIPAA', action: () => navigate({ page: 'security' }) },
      ]
    },
    {
      title: 'Resources',
      links: [
        { label: 'Documentation', action: () => navigate({ page: 'landing' }) },
        { label: 'API Reference', action: () => navigate({ page: 'features', slug: 'api-developer' }) },
        { label: 'Community', action: () => navigate({ page: 'landing' }) },
        { label: 'Changelog', action: () => navigate({ page: 'landing' }) },
      ]
    },
  ]

  return (
    <footer className="bg-white border-t border-border">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-16">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-8">
          <div className="col-span-2 md:col-span-1">
            <DrugOSLogo size="sm" />
            <p className="mt-3 text-sm text-muted-foreground max-w-xs">
              AI-powered drug repurposing for rare and complex diseases.
            </p>
            <div className="flex items-center gap-3 mt-4">
              <Button variant="outline" size="sm" onClick={() => navigate({ page: 'contact' })}>Book a Demo</Button>
            </div>
          </div>
          {footerSections.map(section => (
            <div key={section.title}>
              <h4 className="text-sm font-semibold text-foreground mb-3">{section.title}</h4>
              <ul className="space-y-2">
                {section.links.map(link => (
                  <li key={link.label}>
                    <button onClick={link.action} className="text-sm text-muted-foreground hover:text-foreground transition-colors">
                      {link.label}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <Separator className="my-8" />
        <div className="flex flex-col sm:flex-row items-center justify-between gap-4 text-sm text-muted-foreground">
          <p>&copy; 2026 DrugOS Corp. All rights reserved.</p>
          <div className="flex items-center gap-4">
            <button onClick={() => navigate({ page: 'status' })} className="flex items-center gap-1.5 hover:text-foreground transition-colors">
              <StatusDot status="operational" /> All systems operational
            </button>
          </div>
        </div>
      </div>
    </footer>
  )
}

function PublicLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex flex-col bg-[#F8F8FA]">
      <PublicHeader />
      <main className="flex-1">{children}</main>
      <PublicFooter />
    </div>
  )
}

// =====================================================================
// LANDING PAGE
// =====================================================================

function LandingPage() {
  const { navigate } = useRouter()
  const [searchQuery, setSearchQuery] = useState('')
  const [showAutocomplete, setShowAutocomplete] = useState(false)
  const [selectedDisease, setSelectedDisease] = useState<string | null>(null)

  const filteredDiseases = useMemo(() => {
    if (!searchQuery || searchQuery.length < 2) return []
    return diseases.filter(d =>
      d.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      d.therapeuticArea.toLowerCase().includes(searchQuery.toLowerCase()) ||
      d.icdCode.toLowerCase().includes(searchQuery.toLowerCase())
    ).slice(0, 6)
  }, [searchQuery])

  const handleSearch = () => {
    if (selectedDisease) {
      navigate({ page: 'app', section: 'search', sub: 'results', id: selectedDisease })
    } else if (filteredDiseases.length > 0) {
      navigate({ page: 'app', section: 'search', sub: 'results', id: filteredDiseases[0].id })
    }
  }

  const features = [
    { icon: <Search className="w-6 h-6" />, title: 'Disease Search & Candidate Ranking', desc: 'Search any disease and get AI-ranked drug repurposing candidates with composite scores.', slug: 'disease-search' },
    { icon: <Network className="w-6 h-6" />, title: 'Knowledge Graph Explorer', desc: 'Interactive biomedical knowledge graph with 500K+ nodes and 6M+ edges.', slug: 'knowledge-graph' },
    { icon: <Shield className="w-6 h-6" />, title: 'Safety & Off-Target Profiling', desc: 'Comprehensive safety assessment with contraindication detection.', slug: 'safety-profiling' },
    { icon: <FileText className="w-6 h-6" />, title: 'Evidence Package & Reports', desc: 'Generate regulatory-grade evidence packages with full mechanistic pathways.', slug: 'evidence-reports' },
    { icon: <Code className="w-6 h-6" />, title: 'API & Developer Tools', desc: 'RESTful API with 50K+ daily calls, webhooks, and SDK support.', slug: 'api-developer' },
  ]

  const steps = [
    { num: '01', title: 'Knowledge Graph', desc: '5 node types, 8 edge types, 500K+ nodes from 10+ data sources' },
    { num: '02', title: 'Graph Transformer', desc: 'Heterogeneous GNN scores every drug-disease pair' },
    { num: '03', title: 'Composite Scoring', desc: 'KG + molecular similarity + safety + clinical + IP signals' },
    { num: '04', title: 'Explainable Reports', desc: 'Full mechanistic pathways and evidence packages' },
  ]

  const logos = ['Pfizer', 'Novartis', 'Roche', 'AstraZeneca', 'Biogen', 'Merck']

  return (
    <div>
      {/* Hero Section */}
      <section className="relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-b from-[#5B4FCF]/5 via-transparent to-transparent" />
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-16 sm:pt-24 pb-20">
          <div className="max-w-3xl mx-auto text-center">
            <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
              <Badge variant="secondary" className="mb-6 px-3 py-1 text-sm bg-[#5B4FCF]/10 text-[#5B4FCF] border-[#5B4FCF]/20">
                <Zap className="w-3.5 h-3.5 mr-1" /> Now with GxP Validated Mode
              </Badge>
              <h1 className="text-4xl sm:text-5xl lg:text-6xl font-bold text-foreground leading-tight tracking-tight">
                Find new treatments<br />
                <span className="text-[#5B4FCF]">for any disease.</span> Instantly.
              </h1>
              <p className="mt-6 text-lg sm:text-xl text-muted-foreground max-w-2xl mx-auto leading-relaxed">
                DrugOS uses AI to systematically mine 10,000+ FDA-approved drugs against every known disease using a multi-modal biomedical knowledge graph.
              </p>
            </motion.div>

            {/* Search Bar */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5, delay: 0.2 }}
              className="mt-10 max-w-2xl mx-auto"
            >
              <div className="relative">
                <div className="flex items-center bg-white rounded-2xl shadow-xl shadow-slate-200/60 border border-border p-2">
                  <Search className="w-5 h-5 text-muted-foreground ml-3 mr-2 shrink-0" />
                  <input
                    value={searchQuery}
                    onChange={(e) => { setSearchQuery(e.target.value); setShowAutocomplete(true); setSelectedDisease(null) }}
                    onFocus={() => setShowAutocomplete(true)}
                    onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                    placeholder="Search for a disease — e.g. Huntington's, Alzheimer's, ALS..."
                    className="flex-1 py-3 text-base bg-transparent border-none outline-none placeholder:text-muted-foreground/60"
                  />
                  <Button onClick={handleSearch} className="bg-[#5B4FCF] hover:bg-[#4B3FBF] px-6 py-3 rounded-xl text-base">
                    Search
                  </Button>
                </div>

                {/* Autocomplete Dropdown */}
                <AnimatePresence>
                  {showAutocomplete && filteredDiseases.length > 0 && (
                    <motion.div
                      initial={{ opacity: 0, y: -4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -4 }}
                      className="absolute top-full mt-2 w-full bg-white rounded-xl shadow-xl border border-border overflow-hidden z-50"
                    >
                      {filteredDiseases.map(d => (
                        <button
                          key={d.id}
                          onClick={() => { setSearchQuery(d.name); setSelectedDisease(d.id); setShowAutocomplete(false) }}
                          className="w-full text-left px-5 py-3 hover:bg-accent transition-colors flex items-center justify-between"
                        >
                          <div>
                            <span className="font-medium text-foreground">{d.name}</span>
                            <span className="text-muted-foreground text-sm ml-2">{d.icdCode}</span>
                          </div>
                          <Badge variant="outline" className="text-xs">{d.therapeuticArea}</Badge>
                        </button>
                      ))}
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              {/* Quick Links */}
              <div className="flex items-center justify-center gap-3 mt-4 flex-wrap">
                <span className="text-sm text-muted-foreground">Popular:</span>
                {trendingDiseases.slice(0, 4).map(d => (
                  <button
                    key={d.name}
                    onClick={() => { setSearchQuery(d.name); setSelectedDisease(diseases.find(dd => dd.name === d.name)?.id || null) }}
                    className="text-sm text-[#5B4FCF] hover:underline"
                  >
                    {d.name}
                  </button>
                ))}
              </div>
            </motion.div>

            {/* CTA Buttons */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5, delay: 0.3 }}
              className="mt-8 flex items-center justify-center gap-4 flex-wrap"
            >
              <Button size="lg" onClick={() => navigate({ page: 'register' })} className="bg-[#5B4FCF] hover:bg-[#4B3FBF] text-base px-8">
                Start Free <ArrowRight className="w-4 h-4 ml-1" />
              </Button>
              <Button size="lg" variant="outline" onClick={() => navigate({ page: 'contact' })} className="text-base px-8">
                Book a Demo
              </Button>
            </motion.div>
          </div>

          {/* Stats */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.4 }}
            className="mt-16 grid grid-cols-3 gap-8 max-w-2xl mx-auto text-center"
          >
            {[
              { value: '10,000+', label: 'Drugs Analyzed' },
              { value: '7,000+', label: 'Diseases Covered' },
              { value: '$0', label: 'Cost to Start' },
            ].map(stat => (
              <div key={stat.label}>
                <div className="text-3xl sm:text-4xl font-bold text-foreground">{stat.value}</div>
                <div className="text-sm text-muted-foreground mt-1">{stat.label}</div>
              </div>
            ))}
          </motion.div>
        </div>
      </section>

      {/* How It Works */}
      <section className="py-20 bg-white">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <SectionHeading title="How It Works" subtitle="From disease query to validated candidate in minutes, not months" />
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8">
            {steps.map((step, i) => (
              <motion.div
                key={step.num}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.1 }}
                className="text-center"
              >
                <div className="w-16 h-16 rounded-2xl bg-[#5B4FCF]/10 text-[#5B4FCF] font-bold text-2xl flex items-center justify-center mx-auto mb-4">
                  {step.num}
                </div>
                <h3 className="text-lg font-semibold text-foreground mb-2">{step.title}</h3>
                <p className="text-sm text-muted-foreground">{step.desc}</p>
                {i < steps.length - 1 && (
                  <ArrowRight className="w-5 h-5 text-[#5B4FCF]/30 hidden lg:block absolute right-0 top-1/2 -translate-y-1/2 translate-x-1/2" />
                )}
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* Feature Cards */}
      <section className="py-20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <SectionHeading title="Core Capabilities" subtitle="Everything you need for systematic drug repurposing" />
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {features.map((f, i) => (
              <motion.div
                key={f.title}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.08 }}
              >
                <Card className="h-full hover:shadow-lg transition-shadow cursor-pointer group" onClick={() => navigate({ page: 'features', slug: f.slug })}>
                  <CardHeader>
                    <div className="w-12 h-12 rounded-xl bg-[#5B4FCF]/10 text-[#5B4FCF] flex items-center justify-center mb-2 group-hover:bg-[#5B4FCF] group-hover:text-white transition-colors">
                      {f.icon}
                    </div>
                    <CardTitle className="text-lg">{f.title}</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <CardDescription className="text-sm leading-relaxed">{f.desc}</CardDescription>
                  </CardContent>
                  <CardFooter>
                    <span className="text-sm text-[#5B4FCF] font-medium flex items-center gap-1 group-hover:gap-2 transition-all">
                      Learn more <ArrowRight className="w-3.5 h-3.5" />
                    </span>
                  </CardFooter>
                </Card>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* Customer Logos */}
      <section className="py-16 bg-white">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <p className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-8">Trusted by leading pharmaceutical companies</p>
          <div className="flex items-center justify-center gap-8 sm:gap-16 flex-wrap opacity-40">
            {logos.map(name => (
              <div key={name} className="text-2xl font-bold text-foreground/60 tracking-tight">{name}</div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing Teaser */}
      <section className="py-20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <h2 className="text-3xl font-bold text-foreground">Start free. Scale as you discover.</h2>
          <p className="text-muted-foreground mt-3 text-lg max-w-xl mx-auto">
            From academic researchers to enterprise pharma, we have a plan for every stage.
          </p>
          <div className="flex items-center justify-center gap-4 mt-8">
            <Button size="lg" onClick={() => navigate({ page: 'pricing' })} className="bg-[#5B4FCF] hover:bg-[#4B3FBF]">
              View Pricing <ArrowRight className="w-4 h-4 ml-1" />
            </Button>
            <Button size="lg" variant="outline" onClick={() => navigate({ page: 'contact' })}>
              Talk to Sales
            </Button>
          </div>
        </div>
      </section>

      {/* Bottom CTA */}
      <section className="py-20 bg-gradient-to-br from-[#5B4FCF] to-[#7B6FEF]">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <h2 className="text-3xl sm:text-4xl font-bold text-white">Ready to find your next breakthrough?</h2>
          <p className="text-purple-200 mt-4 text-lg max-w-xl mx-auto">
            Join hundreds of researchers already using DrugOS to discover new therapeutic uses for existing drugs.
          </p>
          <div className="flex items-center justify-center gap-4 mt-8">
            <Button size="lg" onClick={() => navigate({ page: 'register' })} className="bg-white text-[#5B4FCF] hover:bg-slate-50">
              Get Started Free
            </Button>
            <Button size="lg" variant="outline" className="border-white/30 text-white hover:bg-white/10" onClick={() => navigate({ page: 'contact' })}>
              Schedule Demo
            </Button>
          </div>
        </div>
      </section>
    </div>
  )
}

// =====================================================================
// PRICING PAGE
// =====================================================================

function PricingPage() {
  const { navigate } = useRouter()
  const [calcQueries, setCalcQueries] = useState([500])
  const [calcApiCalls, setCalcApiCalls] = useState([25000])
  const [calcSeats, setCalcSeats] = useState([10])
  const [faqOpen, setFaqOpen] = useState<string | null>(null)

  const faqs = [
    { q: 'Can I switch plans at any time?', a: 'Yes, you can upgrade or downgrade your plan at any time. Changes take effect at the start of your next billing cycle.' },
    { q: 'What happens when I exceed my query limit?', a: 'You will receive a warning at 80% usage. After exceeding, additional queries are billed at a per-query overage rate.' },
    { q: 'Is the Free Academic plan really free?', a: 'Yes, the Free Academic plan is completely free for verified academic researchers with a .edu email address. No credit card required.' },
    { q: 'What is the Discovery Deal?', a: 'The Discovery Deal is a licensing arrangement where pharmaceutical companies acquire exclusive rights to a validated drug repurposing candidate identified by DrugOS, including full evidence packages and regulatory support.' },
    { q: 'Do you offer HIPAA compliance?', a: 'Yes, our Professional and Enterprise plans include HIPAA-compliant infrastructure with Business Associate Agreements (BAA) available.' },
    { q: 'Can I try before I buy?', a: 'Absolutely. Start with our Free Academic plan or request a 14-day trial of any paid plan with full feature access.' },
  ]

  const estimatedCost = useMemo(() => {
    const q = calcQueries[0]
    const api = calcApiCalls[0]
    const seats = calcSeats[0]
    if (q <= 10 && api <= 1000 && seats <= 1) return 0
    if (q <= 100 && api <= 1000 && seats <= 5) return 499
    if (q <= 1000 && api <= 50000 && seats <= 25) return 5000
    return 5000 + Math.max(0, (seats - 25) * 100) + Math.max(0, (api - 50000) * 0.001)
  }, [calcQueries, calcApiCalls, calcSeats])

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      <div className="text-center mb-12">
        <h1 className="text-4xl font-bold text-foreground">Simple, transparent pricing</h1>
        <p className="text-lg text-muted-foreground mt-3 max-w-2xl mx-auto">
          Start free for academic research. Scale as your discoveries grow. Enterprise-grade security included.
        </p>
      </div>

      {/* Plan Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4 mb-16">
        {subscriptionPlans.map(plan => (
          <Card
            key={plan.id}
            className={cn(
              'relative hover:shadow-lg transition-shadow',
              plan.id === 'professional' && 'border-[#5B4FCF] ring-2 ring-[#5B4FCF]/20'
            )}
          >
            {plan.id === 'professional' && (
              <div className="absolute -top-3 left-1/2 -translate-x-1/2">
                <Badge className="bg-[#5B4FCF] text-white px-3">Most Popular</Badge>
              </div>
            )}
            <CardHeader className="pb-2">
              <CardTitle className="text-lg">{plan.name}</CardTitle>
              <div className="mt-2">
                <span className="text-3xl font-bold text-foreground">{plan.price}</span>
                <span className="text-muted-foreground text-sm">{plan.period}</span>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-sm text-muted-foreground">{plan.users} users</p>
              <ul className="space-y-2">
                {plan.features.map((f, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm">
                    <Check className="w-4 h-4 text-[#1D9E75] shrink-0 mt-0.5" />
                    <span className="text-muted-foreground">{f}</span>
                  </li>
                ))}
              </ul>
            </CardContent>
            <CardFooter>
              <Button
                className={cn(
                  'w-full',
                  plan.id === 'professional' ? 'bg-[#5B4FCF] hover:bg-[#4B3FBF]' : '',
                  plan.id === 'free' && 'bg-[#1D9E75] hover:bg-[#168F68]'
                )}
                variant={plan.id === 'professional' || plan.id === 'free' ? 'default' : 'outline'}
                onClick={() => navigate({ page: plan.id === 'enterprise' || plan.id === 'discovery' ? 'contact' : 'register' })}
              >
                {plan.id === 'enterprise' || plan.id === 'discovery' ? 'Contact Us' : plan.price === '$0' ? 'Start Free' : 'Get Started'}
              </Button>
            </CardFooter>
          </Card>
        ))}
      </div>

      {/* Cost Calculator */}
      <Card className="mb-16">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-[#5B4FCF]" /> Cost Calculator
          </CardTitle>
          <CardDescription>Estimate your monthly cost based on your expected usage</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div>
            <div className="flex justify-between mb-2">
              <Label>Monthly Queries</Label>
              <span className="text-sm font-medium text-foreground">{calcQueries[0]}</span>
            </div>
            <Slider value={calcQueries} onValueChange={setCalcQueries} min={0} max={5000} step={50} />
          </div>
          <div>
            <div className="flex justify-between mb-2">
              <Label>API Calls / Day</Label>
              <span className="text-sm font-medium text-foreground">{calcApiCalls[0].toLocaleString()}</span>
            </div>
            <Slider value={calcApiCalls} onValueChange={setCalcApiCalls} min={0} max={100000} step={1000} />
          </div>
          <div>
            <div className="flex justify-between mb-2">
              <Label>Team Seats</Label>
              <span className="text-sm font-medium text-foreground">{calcSeats[0]}</span>
            </div>
            <Slider value={calcSeats} onValueChange={setCalcSeats} min={1} max={100} step={1} />
          </div>
          <Separator />
          <div className="flex items-center justify-between">
            <span className="text-lg font-medium">Estimated Monthly Cost</span>
            <span className="text-3xl font-bold text-[#5B4FCF]">
              {estimatedCost === 0 ? 'Free' : `$${estimatedCost.toLocaleString()}/mo`}
            </span>
          </div>
        </CardContent>
      </Card>

      {/* FAQ */}
      <div className="max-w-3xl mx-auto">
        <h2 className="text-2xl font-bold text-foreground text-center mb-8">Frequently Asked Questions</h2>
        <Accordion type="single" collapsible className="space-y-3">
          {faqs.map((faq, i) => (
            <AccordionItem key={i} value={`faq-${i}`} className="bg-white rounded-lg border px-4">
              <AccordionTrigger className="text-left font-medium">{faq.q}</AccordionTrigger>
              <AccordionContent className="text-muted-foreground">{faq.a}</AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>
      </div>
    </div>
  )
}

// =====================================================================
// ABOUT PAGE
// =====================================================================

function AboutPage() {
  const team = [
    { name: 'Manoj Builder', role: 'CEO & Co-Founder', desc: 'Former pharma data scientist. 15+ years in drug discovery.' },
    { name: 'Rohan Analyst', role: 'CTO & Co-Founder', desc: 'ML engineer with deep expertise in graph neural networks.' },
    { name: 'Aseem Hustler', role: 'COO & Co-Founder', desc: 'Serial operator. Built and scaled B2B SaaS companies.' },
  ]

  const milestones = [
    { year: '2024', event: 'DrugOS founded with a mission to democratize drug repurposing' },
    { year: '2025', event: 'Launched MVP with 5,000 drugs and knowledge graph v1' },
    { year: '2025', event: 'First validated prediction confirmed by wet-lab results' },
    { year: '2026', event: 'Series A funding. 10,000+ drugs, enterprise customers onboarded' },
    { year: '2026', event: 'GxP validated mode, HIPAA compliance, Discovery Deal launched' },
  ]

  const press = [
    { outlet: 'Nature Biotechnology', title: 'AI Drug Repurposing Platform Identifies Novel Candidates' },
    { outlet: 'TechCrunch', title: 'DrugOS Raises Series A to Accelerate Drug Repurposing' },
    { outlet: 'STAT News', title: 'New Platform Promises Faster Path to Rare Disease Treatments' },
  ]

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      {/* Mission */}
      <div className="text-center max-w-3xl mx-auto mb-16">
        <h1 className="text-4xl font-bold text-foreground">Building the future of drug repurposing</h1>
        <p className="text-lg text-muted-foreground mt-4 leading-relaxed">
          We believe every disease deserves a chance at a cure. DrugOS uses AI to systematically explore the universe of approved drugs,
          finding new therapeutic uses faster and cheaper than traditional drug discovery. Our mission is to democratize access to
          life-saving treatments — especially for rare and neglected diseases.
        </p>
      </div>

      {/* Team */}
      <SectionHeading title="Leadership Team" subtitle="The people behind DrugOS" />
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-16">
        {team.map(member => (
          <Card key={member.name} className="hover:shadow-md transition-shadow">
            <CardContent className="pt-6 text-center">
              <Avatar className="w-20 h-20 mx-auto mb-4">
                <AvatarFallback className="bg-[#5B4FCF] text-white text-2xl">{member.name.split(' ').map(n => n[0]).join('')}</AvatarFallback>
              </Avatar>
              <h3 className="text-lg font-semibold text-foreground">{member.name}</h3>
              <p className="text-sm text-[#5B4FCF] font-medium">{member.role}</p>
              <p className="text-sm text-muted-foreground mt-2">{member.desc}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Timeline */}
      <SectionHeading title="Milestones" subtitle="Our journey so far" />
      <div className="relative mb-16 pl-8 border-l-2 border-[#5B4FCF]/20 space-y-8 max-w-2xl">
        {milestones.map(m => (
          <div key={m.year + m.event} className="relative">
            <div className="absolute -left-[2.55rem] w-4 h-4 rounded-full bg-[#5B4FCF] border-4 border-[#F8F8FA]" />
            <span className="text-sm font-bold text-[#5B4FCF]">{m.year}</span>
            <p className="text-foreground mt-0.5">{m.event}</p>
          </div>
        ))}
      </div>

      {/* Press */}
      <SectionHeading title="In the News" subtitle="What they're saying about DrugOS" />
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {press.map(article => (
          <Card key={article.title} className="hover:shadow-md transition-shadow cursor-pointer">
            <CardContent className="pt-6">
              <Badge variant="secondary" className="mb-3">{article.outlet}</Badge>
              <h3 className="font-semibold text-foreground leading-snug">{article.title}</h3>
              <p className="text-sm text-[#5B4FCF] mt-3 flex items-center gap-1">
                Read more <ArrowRight className="w-3 h-3" />
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

// =====================================================================
// SECURITY PAGE
// =====================================================================

function SecurityPage() {
  const certifications = [
    { name: 'SOC 2 Type II', desc: 'Annual audit confirming security controls', icon: <ShieldCheck className="w-8 h-8" /> },
    { name: 'HIPAA Compliant', desc: 'Full compliance with BAAs available', icon: <Heart className="w-8 h-8" /> },
    { name: '21 CFR Part 11', desc: 'GxP validated mode for FDA submissions', icon: <FileCheck className="w-8 h-8" /> },
    { name: 'GDPR Compliant', desc: 'EU data protection regulation compliance', icon: <Globe2 className="w-8 h-8" /> },
  ]

  const encryption = [
    { title: 'Data at Rest', desc: 'AES-256 encryption for all stored data' },
    { title: 'Data in Transit', desc: 'TLS 1.3 for all network communications' },
    { title: 'Key Management', desc: 'AWS KMS with customer-managed keys' },
    { title: 'Field-Level Encryption', desc: 'PHI fields encrypted separately' },
  ]

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      <div className="text-center max-w-3xl mx-auto mb-16">
        <h1 className="text-4xl font-bold text-foreground">Security & Trust</h1>
        <p className="text-lg text-muted-foreground mt-4 leading-relaxed">
          DrugOS is built with security-first principles. Your research data is protected by enterprise-grade encryption, compliance frameworks, and rigorous access controls.
        </p>
      </div>

      {/* Certifications */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6 mb-16">
        {certifications.map(cert => (
          <Card key={cert.name} className="text-center hover:shadow-md transition-shadow">
            <CardContent className="pt-6">
              <div className="w-16 h-16 rounded-2xl bg-[#1D9E75]/10 text-[#1D9E75] flex items-center justify-center mx-auto mb-4">
                {cert.icon}
              </div>
              <h3 className="text-lg font-semibold text-foreground">{cert.name}</h3>
              <p className="text-sm text-muted-foreground mt-1">{cert.desc}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Encryption */}
      <SectionHeading title="Encryption & Data Protection" />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-16">
        {encryption.map(e => (
          <Card key={e.title}>
            <CardContent className="pt-6 flex items-start gap-4">
              <Lock className="w-6 h-6 text-[#5B4FCF] shrink-0 mt-0.5" />
              <div>
                <h3 className="font-semibold text-foreground">{e.title}</h3>
                <p className="text-sm text-muted-foreground mt-1">{e.desc}</p>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Data Residency */}
      <SectionHeading title="Data Residency" subtitle="Your data stays where you need it" />
      <Card className="mb-16">
        <CardContent className="pt-6">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {[
              { region: 'US-East', desc: 'Virginia, USA', flag: '🇺🇸' },
              { region: 'EU-West', desc: 'Frankfurt, Germany', flag: '🇩🇪' },
              { region: 'APAC', desc: 'Singapore', flag: '🇸🇬' },
            ].map(r => (
              <div key={r.region} className="flex items-center gap-3 p-4 rounded-xl bg-accent">
                <span className="text-2xl">{r.flag}</span>
                <div>
                  <p className="font-medium text-foreground">{r.region}</p>
                  <p className="text-sm text-muted-foreground">{r.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Resources */}
      <SectionHeading title="Downloadable Resources" />
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          { name: 'SOC 2 Report', icon: <FileText className="w-5 h-5" /> },
          { name: 'HIPAA BAA Template', icon: <FileText className="w-5 h-5" /> },
          { name: 'Security Whitepaper', icon: <FileText className="w-5 h-5" /> },
          { name: 'Penetration Test Summary', icon: <FileText className="w-5 h-5" /> },
        ].map(r => (
          <Card key={r.name} className="cursor-pointer hover:shadow-md transition-shadow">
            <CardContent className="pt-6 flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-[#5B4FCF]/10 text-[#5B4FCF] flex items-center justify-center">{r.icon}</div>
              <div>
                <p className="font-medium text-foreground text-sm">{r.name}</p>
                <p className="text-xs text-[#5B4FCF] flex items-center gap-1">Download <Download className="w-3 h-3" /></p>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

// =====================================================================
// STATUS PAGE
// =====================================================================

function StatusPage() {
  // Pull real service availability from /api/system/status.
  const { data, loading, error, refetch } = useSystemStatus()

  const services = data
    ? Object.entries(data.services).map(([key, s]) => ({
        key,
        name: s.service,
        available: s.available,
        description: s.description,
        reason: s.reason,
      }))
    : []

  const operationalCount = services.filter(s => s.available).length
  const overallStatus = services.length === 0
    ? 'unknown'
    : operationalCount === services.length ? 'operational' : 'degraded'

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      <div className="text-center mb-12">
        <h1 className="text-4xl font-bold text-foreground">System Status</h1>
        <p className="text-lg text-muted-foreground mt-3">Real-time service health monitoring</p>
        <Button variant="outline" size="sm" className="mt-4" onClick={() => refetch()} disabled={loading}>
          <RefreshCw className={cn('w-4 h-4 mr-2', loading && 'animate-spin')} />
          Refresh
        </Button>
      </div>

      {/* Overall Status */}
      <Card className={cn('mb-8', overallStatus === 'operational' ? 'border-[#1D9E75]' : overallStatus === 'unknown' ? 'border-[#D4853A]' : 'border-[#D4853A]')}>
        <CardContent className="pt-6 flex items-center gap-4">
          <div className={cn(
            'w-14 h-14 rounded-full flex items-center justify-center',
            overallStatus === 'operational' ? 'bg-[#1D9E75]/10 text-[#1D9E75]' : 'bg-[#D4853A]/10 text-[#D4853A]'
          )}>
            {loading ? <RefreshCw className="w-7 h-7 animate-spin" /> : overallStatus === 'operational' ? <CheckCircle2 className="w-7 h-7" /> : <AlertTriangle className="w-7 h-7" />}
          </div>
          <div>
            <h2 className="text-xl font-bold text-foreground">
              {loading ? 'Checking services…' : overallStatus === 'operational' ? 'All Systems Operational' : overallStatus === 'unknown' ? 'Unable to reach status API' : 'Partial Service Degradation'}
            </h2>
            <p className="text-muted-foreground">
              {error ? `Error: ${error}` : data ? `Last updated: ${new Date(data.generatedAt).toLocaleString()}` : 'Loading…'}
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Service List — REAL backend availability */}
      <Card className="mb-8">
        <CardContent className="pt-6">
          <div className="space-y-4">
            {services.map(service => (
              <div key={service.key} className="flex items-start justify-between py-3 border-b border-border last:border-0 gap-4">
                <div className="flex items-start gap-3 flex-1">
                  <StatusDot status={service.available ? 'operational' : 'degraded'} />
                  <div className="flex-1 min-w-0">
                    <span className="font-medium text-foreground block">{service.name}</span>
                    {service.description && (
                      <p className="text-xs text-muted-foreground mt-0.5">{service.description}</p>
                    )}
                    {!service.available && service.reason && (
                      <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mt-1.5">
                        {service.reason}
                      </p>
                    )}
                  </div>
                </div>
                <Badge variant={service.available ? 'secondary' : 'destructive'} className="text-xs shrink-0">
                  {service.available ? 'available' : 'not configured'}
                </Badge>
              </div>
            ))}
            {services.length === 0 && !loading && (
              <div className="text-center py-6 text-sm text-muted-foreground">
                No services reported. <button className="text-[#5B4FCF] underline" onClick={() => refetch()}>Retry</button>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Incident History */}
      <Card>
        <CardHeader>
          <CardTitle>Incident History</CardTitle>
          <CardDescription>Recent incidents and resolutions</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {[
              { date: 'Jun 8, 2026', title: 'Report Generator Degraded Performance', status: 'Resolved', duration: '2h 15m' },
              { date: 'May 22, 2026', title: 'API Gateway Intermittent 503 Errors', status: 'Resolved', duration: '45m' },
              { date: 'May 10, 2026', title: 'Scheduled Maintenance - Knowledge Graph Reindex', status: 'Completed', duration: '4h' },
            ].map(incident => (
              <div key={incident.title} className="flex items-start gap-3 py-3 border-b border-border last:border-0">
                <CheckCircle2 className="w-5 h-5 text-[#1D9E75] shrink-0 mt-0.5" />
                <div className="flex-1">
                  <p className="font-medium text-foreground">{incident.title}</p>
                  <div className="flex items-center gap-3 mt-1 text-sm text-muted-foreground">
                    <span>{incident.date}</span>
                    <span>Duration: {incident.duration}</span>
                    <Badge variant="secondary" className="text-xs">{incident.status}</Badge>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

// =====================================================================
// BLOG PAGE
// =====================================================================

function BlogPage() {
  const [activeCategory, setActiveCategory] = useState('All')
  const categories = ['All', 'Research', 'Technology', 'Partnerships']
  const filtered = activeCategory === 'All' ? blogPosts : blogPosts.filter(p => p.category === activeCategory)

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      <div className="text-center mb-12">
        <h1 className="text-4xl font-bold text-foreground">Blog & News</h1>
        <p className="text-lg text-muted-foreground mt-3">Latest updates from DrugOS research and engineering</p>
      </div>

      {/* Category Tabs */}
      <div className="flex items-center gap-2 mb-8 flex-wrap">
        {categories.map(cat => (
          <Button
            key={cat}
            variant={activeCategory === cat ? 'default' : 'outline'}
            size="sm"
            onClick={() => setActiveCategory(cat)}
            className={activeCategory === cat ? 'bg-[#5B4FCF] hover:bg-[#4B3FBF]' : ''}
          >
            {cat}
          </Button>
        ))}
      </div>

      {/* Blog Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {filtered.map(post => (
          <Card key={post.id} className="hover:shadow-lg transition-shadow cursor-pointer">
            <CardContent className="pt-6">
              <div className="flex items-center gap-2 mb-3">
                <Badge variant="secondary">{post.category}</Badge>
                <span className="text-xs text-muted-foreground">{post.date}</span>
              </div>
              <h3 className="text-lg font-semibold text-foreground leading-snug mb-2">{post.title}</h3>
              <p className="text-sm text-muted-foreground leading-relaxed">{post.excerpt}</p>
              <p className="text-sm text-[#5B4FCF] mt-4 flex items-center gap-1">
                Read more <ArrowRight className="w-3 h-3" />
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

// =====================================================================
// CONTACT PAGE
// =====================================================================

function ContactPage() {
  const [formData, setFormData] = useState({ name: '', email: '', company: '', message: '', inquiryType: '' })

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      <div className="text-center mb-12">
        <h1 className="text-4xl font-bold text-foreground">Get in Touch</h1>
        <p className="text-lg text-muted-foreground mt-3">We'd love to hear from you. Let us know how we can help.</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-12">
        {/* Contact Form */}
        <Card>
          <CardHeader>
            <CardTitle>Send us a message</CardTitle>
            <CardDescription>Fill out the form and we'll get back to you within 24 hours.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label htmlFor="contact-name">Name</Label>
                <Input id="contact-name" placeholder="Your name" value={formData.name} onChange={e => setFormData({ ...formData, name: e.target.value })} />
              </div>
              <div>
                <Label htmlFor="contact-email">Email</Label>
                <Input id="contact-email" type="email" placeholder="you@company.com" value={formData.email} onChange={e => setFormData({ ...formData, email: e.target.value })} />
              </div>
            </div>
            <div>
              <Label htmlFor="contact-company">Company</Label>
              <Input id="contact-company" placeholder="Your organization" value={formData.company} onChange={e => setFormData({ ...formData, company: e.target.value })} />
            </div>
            <div>
              <Label>Inquiry Type</Label>
              <Select value={formData.inquiryType} onValueChange={v => setFormData({ ...formData, inquiryType: v })}>
                <SelectTrigger><SelectValue placeholder="Select inquiry type" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="sales">Sales Inquiry</SelectItem>
                  <SelectItem value="support">Technical Support</SelectItem>
                  <SelectItem value="partnership">Partnership</SelectItem>
                  <SelectItem value="academic">Academic Access</SelectItem>
                  <SelectItem value="other">Other</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label htmlFor="contact-message">Message</Label>
              <Textarea id="contact-message" placeholder="Tell us about your needs..." rows={4} value={formData.message} onChange={e => setFormData({ ...formData, message: e.target.value })} />
            </div>
            <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]">Send Message</Button>
          </CardContent>
        </Card>

        {/* Office Info */}
        <div className="space-y-6">
          <Card>
            <CardContent className="pt-6">
              <div className="h-48 bg-slate-100 rounded-lg flex items-center justify-center mb-4">
                <MapPin className="w-12 h-12 text-muted-foreground/30" />
              </div>
            </CardContent>
          </Card>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {[
              { city: 'San Francisco', address: '535 Mission St, Suite 1400', icon: <Building className="w-5 h-5" /> },
              { city: 'Boston', address: '50 Milk St, Floor 16', icon: <Building className="w-5 h-5" /> },
            ].map(office => (
              <Card key={office.city}>
                <CardContent className="pt-6">
                  <div className="flex items-center gap-2 mb-2 text-[#5B4FCF]">{office.icon}<span className="font-semibold">{office.city}</span></div>
                  <p className="text-sm text-muted-foreground">{office.address}</p>
                </CardContent>
              </Card>
            ))}
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Partner with DrugOS</CardTitle>
              <CardDescription>Interested in integrating DrugOS into your workflow?</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground mb-4">
                We work with pharmaceutical companies, CROs, academic institutions, and rare disease foundations.
              </p>
              <Button variant="outline" className="w-full">Learn About Partnerships</Button>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}

// =====================================================================
// CAREERS PAGE
// =====================================================================

function CareersPage() {
  const { navigate } = useRouter()
  const benefits = [
    { icon: <Heart className="w-5 h-5" />, title: 'Health & Wellness', desc: 'Comprehensive medical, dental, vision' },
    { icon: <Globe className="w-5 h-5" />, title: 'Remote-First', desc: 'Work from anywhere in the world' },
    { icon: <GraduationCap className="w-5 h-5" />, title: 'Learning Budget', desc: '$5,000/year for conferences & courses' },
    { icon: <Briefcase className="w-5 h-5" />, title: 'Equity', desc: 'Stock options for all team members' },
  ]

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      <div className="text-center mb-16">
        <h1 className="text-4xl font-bold text-foreground">Join the Team</h1>
        <p className="text-lg text-muted-foreground mt-3 max-w-2xl mx-auto">
          Help us build the future of drug repurposing. We're looking for passionate people who want to make a real impact on human health.
        </p>
      </div>

      {/* Culture */}
      <div className="bg-gradient-to-br from-[#5B4FCF] to-[#7B6FEF] rounded-2xl p-8 sm:p-12 text-white mb-16">
        <h2 className="text-2xl font-bold mb-4">Our Culture</h2>
        <p className="text-purple-200 text-lg max-w-2xl leading-relaxed">
          We move fast, think rigorously, and care deeply. Every line of code and every model inference could lead to a life-saving treatment.
          We take that responsibility seriously — and we have fun doing it.
        </p>
      </div>

      {/* Benefits */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-16">
        {benefits.map(b => (
          <Card key={b.title} className="hover:shadow-md transition-shadow">
            <CardContent className="pt-6">
              <div className="w-10 h-10 rounded-lg bg-[#5B4FCF]/10 text-[#5B4FCF] flex items-center justify-center mb-3">{b.icon}</div>
              <h3 className="font-semibold text-foreground">{b.title}</h3>
              <p className="text-sm text-muted-foreground mt-1">{b.desc}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Job Listings */}
      <SectionHeading title="Open Positions" subtitle="Find your next role" />
      <div className="space-y-4">
        {careers.map(job => (
          <Card key={job.id} className="hover:shadow-md transition-shadow">
            <CardContent className="pt-6 flex items-center justify-between flex-wrap gap-4">
              <div>
                <h3 className="font-semibold text-foreground text-lg">{job.title}</h3>
                <div className="flex items-center gap-3 mt-2 text-sm text-muted-foreground">
                  <span className="flex items-center gap-1"><MapPin className="w-3.5 h-3.5" />{job.location}</span>
                  <span className="flex items-center gap-1"><Clock className="w-3.5 h-3.5" />{job.type}</span>
                  <Badge variant="secondary">{job.department}</Badge>
                </div>
              </div>
              <Button className="bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'contact' })}>Apply Now</Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

// =====================================================================
// CASE STUDIES PAGE
// =====================================================================

function CaseStudiesPage() {
  const studies = [
    {
      type: 'Academic Research',
      org: 'University Neuroscience Lab',
      disease: "Huntington's Disease",
      outcomes: ['Identified 3 novel candidates in 2 weeks', 'Validated Memantine + Riluzole combination', 'Published in Nature Communications'],
      metrics: { time: '2 weeks', candidates: '10', topScore: '87' },
      quote: '"DrugOS compressed 6 months of literature review into 2 weeks of computational analysis."',
      author: '— Dr. Priya Sharma, Principal Investigator'
    },
    {
      type: 'Biotech Startup',
      org: 'NeuroGen Therapeutics',
      disease: 'ALS (Lou Gehrig\'s Disease)',
      outcomes: ['Discovered 5 repurposing candidates', 'Filed 2 provisional patents', 'Raised $12M Series A'],
      metrics: { time: '1 month', candidates: '12', topScore: '82' },
      quote: '"The evidence packages from DrugOS were instrumental in securing our Series A funding."',
      author: '— James Miller, CTO'
    },
    {
      type: 'Pharmaceutical Company',
      org: 'Top-10 Pharma',
      disease: "Pancreatic Cancer",
      outcomes: ['Prioritized 3 lead candidates', 'Advanced 1 to Phase II', 'Reduced discovery cost by 85%'],
      metrics: { time: '3 months', candidates: '8', topScore: '79' },
      quote: '"DrugOS transformed our early-stage pipeline strategy with data-driven insights."',
      author: '— VP of Drug Discovery'
    },
  ]

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      <div className="text-center mb-16">
        <h1 className="text-4xl font-bold text-foreground">Case Studies</h1>
        <p className="text-lg text-muted-foreground mt-3 max-w-2xl mx-auto">
          See how researchers and companies are using DrugOS to accelerate drug repurposing.
        </p>
      </div>

      <div className="space-y-8">
        {studies.map(study => (
          <Card key={study.org} className="overflow-hidden hover:shadow-lg transition-shadow">
            <CardContent className="pt-6">
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
                <div className="lg:col-span-2">
                  <div className="flex items-center gap-2 mb-3">
                    <Badge className="bg-[#5B4FCF] text-white">{study.type}</Badge>
                    <span className="text-sm text-muted-foreground">{study.org}</span>
                  </div>
                  <h3 className="text-xl font-bold text-foreground mb-1">{study.disease}</h3>
                  <ul className="space-y-2 mt-4">
                    {study.outcomes.map((o, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm">
                        <Check className="w-4 h-4 text-[#1D9E75] shrink-0 mt-0.5" />
                        <span className="text-foreground">{o}</span>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-6 p-4 bg-accent rounded-xl">
                    <p className="text-sm italic text-muted-foreground">{study.quote}</p>
                    <p className="text-sm font-medium text-foreground mt-2">{study.author}</p>
                  </div>
                </div>
                <div className="space-y-4">
                  {[
                    { label: 'Time to Results', value: study.metrics.time },
                    { label: 'Candidates Found', value: study.metrics.candidates },
                    { label: 'Top Score', value: study.metrics.topScore },
                  ].map(m => (
                    <div key={m.label} className="p-4 rounded-xl bg-accent text-center">
                      <p className="text-2xl font-bold text-[#5B4FCF]">{m.value}</p>
                      <p className="text-sm text-muted-foreground">{m.label}</p>
                    </div>
                  ))}
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

// =====================================================================
// FEATURE DEEP-DIVE PAGES
// =====================================================================

function FeaturePage({ slug }: { slug: string }) {
  const { navigate } = useRouter()

  const featureData: Record<string, { title: string; subtitle: string; icon: React.ReactNode; description: string; useCases: string[]; highlights: string[] }> = {
    'disease-search': {
      title: 'Disease Search & Candidate Ranking',
      subtitle: 'Find the best drug repurposing candidates for any disease',
      icon: <Search className="w-8 h-8" />,
      description: 'Search any disease from our database of 7,000+ conditions and instantly receive AI-ranked drug repurposing candidates. Our composite scoring algorithm combines knowledge graph signals, molecular similarity, safety profiles, clinical evidence, and IP status into a single actionable score.',
      useCases: ['Rare disease drug discovery', 'Orphan drug identification', 'Combination therapy exploration', 'Pipeline gap analysis'],
      highlights: ['Composite score with 5 signal types', 'Filter by safety tier, phase, IP status', 'Export results as CSV or PDF', 'Save and compare queries over time'],
    },
    'knowledge-graph': {
      title: 'Knowledge Graph Explorer',
      subtitle: 'Interactive biomedical knowledge graph with 500K+ nodes',
      icon: <Network className="w-8 h-8" />,
      description: 'Explore the DrugOS knowledge graph interactively. Visualize relationships between drugs, diseases, genes, proteins, and pathways. Our graph integrates data from 10+ sources including DrugBank, ChEMBL, OpenTargets, and STRING.',
      useCases: ['Mechanism of action exploration', 'Target identification', 'Pathway analysis', 'Drug-target-disease mapping'],
      highlights: ['5 node types, 8 edge types', 'Evidence-weighted edges', 'Interactive force-directed layout', 'Drill-down from any node'],
    },
    'safety-profiling': {
      title: 'Safety & Off-Target Profiling',
      subtitle: 'Comprehensive safety assessment with contraindication detection',
      icon: <Shield className="w-8 h-8" />,
      description: 'Assess the safety profile of any repurposing candidate with our multi-dimensional safety scoring. Detect contraindications, off-target effects, and drug-drug interactions relevant to the target disease population.',
      useCases: ['Contraindication screening', 'Off-target effect prediction', 'Drug-drug interaction checking', 'Population-specific safety assessment'],
      highlights: ['Green/Yellow/Red safety tiers', 'Contraindication alerts', 'Off-target prediction', 'Population-specific warnings'],
    },
    'evidence-reports': {
      title: 'Evidence Package & Reports',
      subtitle: 'Generate regulatory-grade evidence packages',
      icon: <FileText className="w-8 h-8" />,
      description: 'Assemble comprehensive evidence packages for any drug-disease pair. Generate regulatory-grade reports with full mechanistic pathways, clinical evidence summaries, safety assessments, and IP status — ready for internal review or regulatory submission.',
      useCases: ['Regulatory submission support', 'Internal review packages', 'Grant proposal evidence', 'Investor due diligence'],
      highlights: ['Full mechanistic pathway documentation', 'Clinical evidence synthesis', 'IP and patent landscape', 'GxP validated mode available'],
    },
    'api-developer': {
      title: 'API & Developer Tools',
      subtitle: 'Integrate DrugOS into your workflow with our RESTful API',
      icon: <Code className="w-8 h-8" />,
      description: 'Access DrugOS programmatically with our RESTful API. Search diseases, retrieve candidates, generate reports, and set up webhooks — all from your own applications. SDKs available for Python, R, and JavaScript.',
      useCases: ['Pipeline automation', 'Batch disease querying', 'Custom dashboard integration', 'ML model augmentation'],
      highlights: ['50K+ API calls/day (Professional)', 'Webhooks for async events', 'Python, R, JS SDKs', 'Interactive API playground'],
    },
  }

  const feature = featureData[slug]
  if (!feature) return <div className="p-8 text-center"><h1 className="text-2xl font-bold">Feature not found</h1></div>

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12 lg:py-20">
      {/* Hero */}
      <div className="max-w-3xl mb-16">
        <div className="w-16 h-16 rounded-2xl bg-[#5B4FCF]/10 text-[#5B4FCF] flex items-center justify-center mb-6">{feature.icon}</div>
        <h1 className="text-4xl font-bold text-foreground">{feature.title}</h1>
        <p className="text-xl text-muted-foreground mt-3">{feature.subtitle}</p>
        <p className="text-lg text-muted-foreground mt-6 leading-relaxed">{feature.description}</p>
        <div className="flex items-center gap-4 mt-8">
          <Button size="lg" className="bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'register' })}>
            Get Started <ArrowRight className="w-4 h-4 ml-1" />
          </Button>
          <Button size="lg" variant="outline" onClick={() => navigate({ page: 'contact' })}>Talk to Sales</Button>
        </div>
      </div>

      {/* Screenshot Placeholder */}
      <Card className="mb-16 overflow-hidden">
        <div className="h-64 sm:h-80 bg-gradient-to-br from-[#5B4FCF]/5 to-[#5B4FCF]/10 flex items-center justify-center">
          <div className="text-center">
            <feature.icon.type className="w-16 h-16 text-[#5B4FCF]/30 mx-auto mb-4" />
            <p className="text-muted-foreground">Interactive Demo Preview</p>
          </div>
        </div>
      </Card>

      {/* Use Cases & Highlights */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        <Card>
          <CardHeader>
            <CardTitle>Use Cases</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-3">
              {feature.useCases.map((uc, i) => (
                <li key={i} className="flex items-start gap-2">
                  <Target className="w-4 h-4 text-[#5B4FCF] shrink-0 mt-1" />
                  <span className="text-foreground">{uc}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Key Highlights</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-3">
              {feature.highlights.map((h, i) => (
                <li key={i} className="flex items-start gap-2">
                  <Check className="w-4 h-4 text-[#1D9E75] shrink-0 mt-1" />
                  <span className="text-foreground">{h}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>

      {/* Bottom CTA */}
      <div className="mt-16 bg-gradient-to-br from-[#5B4FCF] to-[#7B6FEF] rounded-2xl p-8 sm:p-12 text-center text-white">
        <h2 className="text-2xl sm:text-3xl font-bold">Ready to try {feature.title}?</h2>
        <p className="text-purple-200 mt-3 text-lg">Start free today — no credit card required.</p>
        <div className="flex items-center justify-center gap-4 mt-6">
          <Button size="lg" className="bg-white text-[#5B4FCF] hover:bg-slate-50" onClick={() => navigate({ page: 'register' })}>Start Free</Button>
          <Button size="lg" variant="outline" className="border-white/30 text-white hover:bg-white/10" onClick={() => navigate({ page: 'pricing' })}>View Pricing</Button>
        </div>
      </div>
    </div>
  )
}

// =====================================================================
// AUTH PAGES
// =====================================================================

function AuthLayout({ children, title, subtitle }: { children: React.ReactNode; title: string; subtitle?: string }) {
  const { navigate } = useRouter()
  return (
    <div className="min-h-screen flex flex-col bg-[#F8F8FA]">
      <div className="flex-1 flex items-center justify-center px-4 py-12">
        <div className="w-full max-w-md">
          <div className="text-center mb-8">
            <button onClick={() => navigate({ page: 'landing' })} className="inline-block mb-6">
              <DrugOSLogo size="md" />
            </button>
            <h1 className="text-2xl font-bold text-foreground">{title}</h1>
            {subtitle && <p className="text-muted-foreground mt-1">{subtitle}</p>}
          </div>
          {children}
        </div>
      </div>
    </div>
  )
}

function LoginPage() {
  const { navigate } = useRouter()
  const { login } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setError(null)
    if (!email.trim() || !password) {
      setError('Email and password are required.')
      return
    }
    setSubmitting(true)
    const result = await login(email.trim(), password)
    setSubmitting(false)
    if (result.ok) {
      navigate({ page: 'app', section: 'dashboard' })
    } else {
      setError(result.error || 'Login failed')
    }
  }

  return (
    <AuthLayout title="Welcome back" subtitle="Sign in to your DrugOS account">
      <Card>
        <CardContent className="pt-6 space-y-4">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-md px-3 py-2">
              {error}
            </div>
          )}
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label htmlFor="login-email">Email</Label>
              <Input
                id="login-email"
                type="email"
                autoComplete="email"
                placeholder="you@company.com"
                value={email}
                onChange={e => setEmail(e.target.value)}
                disabled={submitting}
                required
              />
            </div>
            <div>
              <div className="flex items-center justify-between mb-1">
                <Label htmlFor="login-password">Password</Label>
                <button type="button" onClick={() => navigate({ page: 'forgot-password' })} className="text-xs text-[#5B4FCF] hover:underline">Forgot password?</button>
              </div>
              <Input
                id="login-password"
                type="password"
                autoComplete="current-password"
                placeholder="Enter your password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                disabled={submitting}
                required
              />
            </div>
            <Button type="submit" className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" disabled={submitting}>
              {submitting ? 'Signing in…' : 'Sign In'}
            </Button>
          </form>
          <Separator />
          <Button variant="outline" className="w-full" disabled title="SSO is configured at the organization level. Contact your administrator.">
            Continue with SSO
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            Don&apos;t have an account?{' '}
            <button onClick={() => navigate({ page: 'register' })} className="text-[#5B4FCF] font-medium hover:underline">Sign up</button>
          </p>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function RegisterPage() {
  const { navigate } = useRouter()
  const { register } = useAuth()
  const [form, setForm] = useState({ firstName: '', lastName: '', email: '', password: '', organization: '', role: '' })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setError(null)

    // Client-side validation — mirrors the backend's policy.
    if (!form.firstName.trim() || !form.lastName.trim()) {
      setError('First and last name are required.')
      return
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim())) {
      setError('Please enter a valid email address.')
      return
    }
    if (form.password.length < 10) {
      setError('Password must be at least 10 characters long.')
      return
    }
    if (!/[a-z]/.test(form.password) || !/[A-Z]/.test(form.password) || !/[0-9]/.test(form.password) || !/[^A-Za-z0-9]/.test(form.password)) {
      setError('Password must contain at least one uppercase letter, one lowercase letter, one digit, and one symbol.')
      return
    }

    setSubmitting(true)
    const result = await register({
      email: form.email.trim(),
      password: form.password,
      name: `${form.firstName.trim()} ${form.lastName.trim()}`,
      organizationName: form.organization.trim() || undefined,
    })
    setSubmitting(false)
    if (result.ok) {
      // Backend created the user, set cookies, and returned the new session.
      // Send the user straight to the app — no onboarding maze.
      navigate({ page: 'app', section: 'dashboard' })
    } else {
      setError(result.error || 'Registration failed')
    }
  }

  return (
    <AuthLayout title="Create your account" subtitle="Start discovering new treatments today">
      <Card>
        <CardContent className="pt-6 space-y-4">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-md px-3 py-2">
              {error}
            </div>
          )}
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label>First Name</Label>
                <Input placeholder="Manoj" value={form.firstName} onChange={e => setForm({ ...form, firstName: e.target.value })} disabled={submitting} required />
              </div>
              <div>
                <Label>Last Name</Label>
                <Input placeholder="Pagadala" value={form.lastName} onChange={e => setForm({ ...form, lastName: e.target.value })} disabled={submitting} required />
              </div>
            </div>
            <div>
              <Label>Email</Label>
              <Input type="email" autoComplete="email" placeholder="you@university.edu" value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} disabled={submitting} required />
            </div>
            <div>
              <Label>Password</Label>
              <Input type="password" autoComplete="new-password" placeholder="Min 10 chars, mixed case + digit + symbol" value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} disabled={submitting} required />
              <p className="text-xs text-muted-foreground mt-1">At least 10 characters with upper &amp; lower case, a digit, and a symbol.</p>
            </div>
            <div>
              <Label>Organization</Label>
              <Input placeholder="University or Company" value={form.organization} onChange={e => setForm({ ...form, organization: e.target.value })} disabled={submitting} />
            </div>
            <div>
              <Label>Role</Label>
              <Select value={form.role} onValueChange={v => setForm({ ...form, role: v })}>
                <SelectTrigger disabled={submitting}><SelectValue placeholder="Select your role" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="researcher">Researcher</SelectItem>
                  <SelectItem value="data-scientist">Data Scientist</SelectItem>
                  <SelectItem value="pi">Principal Investigator</SelectItem>
                  <SelectItem value="admin">Admin</SelectItem>
                  <SelectItem value="business-dev">Business Development</SelectItem>
                  <SelectItem value="other">Other</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Button type="submit" className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" disabled={submitting}>
              {submitting ? 'Creating account…' : 'Create Account'}
            </Button>
          </form>
          <p className="text-center text-sm text-muted-foreground">
            Already have an account?{' '}
            <button onClick={() => navigate({ page: 'login' })} className="text-[#5B4FCF] font-medium hover:underline">Sign in</button>
          </p>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [sent, setSent] = useState(false)

  return (
    <AuthLayout title="Reset your password" subtitle="We'll send you a reset link">
      <Card>
        <CardContent className="pt-6 space-y-4">
          {!sent ? (
            <>
              <div>
                <Label>Email Address</Label>
                <Input type="email" placeholder="you@company.com" value={email} onChange={e => setEmail(e.target.value)} />
              </div>
              <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => setSent(true)}>Send Reset Link</Button>
            </>
          ) : (
            <div className="text-center py-4">
              <div className="w-14 h-14 rounded-full bg-[#1D9E75]/10 text-[#1D9E75] flex items-center justify-center mx-auto mb-4">
                <Mail className="w-7 h-7" />
              </div>
              <p className="font-semibold text-foreground">Check your email</p>
              <p className="text-sm text-muted-foreground mt-1">We sent a reset link to {email || 'your email'}</p>
            </div>
          )}
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function ResetPasswordPage() {
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')

  return (
    <AuthLayout title="Set new password" subtitle="Choose a strong password for your account">
      <Card>
        <CardContent className="pt-6 space-y-4">
          <div>
            <Label>New Password</Label>
            <Input type="password" placeholder="Min 12 characters" value={password} onChange={e => setPassword(e.target.value)} />
          </div>
          <div>
            <Label>Confirm Password</Label>
            <Input type="password" placeholder="Repeat password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} />
          </div>
          <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]">Reset Password</Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function MFAChallengePage() {
  const [otp, setOtp] = useState(['', '', '', '', '', ''])
  const { navigate } = useRouter()

  const handleOtpChange = (index: number, value: string) => {
    const newOtp = [...otp]
    newOtp[index] = value.slice(-1)
    setOtp(newOtp)
    if (value && index < 5) {
      const next = document.getElementById(`otp-${index + 1}`)
      next?.focus()
    }
  }

  return (
    <AuthLayout title="Two-Factor Authentication" subtitle="Enter the 6-digit code from your authenticator app">
      <Card>
        <CardContent className="pt-6">
          <p className="text-sm text-muted-foreground text-center mb-4">Enter the 6-digit code from your authenticator</p>
          <div className="flex gap-2 justify-center mb-6">
            {otp.map((digit, i) => (
              <Input
                key={i}
                id={`otp-${i}`}
                className="w-12 h-14 text-center text-xl font-bold"
                maxLength={1}
                value={digit}
                onChange={e => handleOtpChange(i, e.target.value)}
              />
            ))}
          </div>
          <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'app', section: 'dashboard' })}>Verify</Button>
          <p className="text-center text-sm text-muted-foreground mt-4">
            Didn&apos;t receive a code? <button className="text-[#5B4FCF] hover:underline">Resend</button>
          </p>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function EmailVerificationPage() {
  const { navigate } = useRouter()
  return (
    <AuthLayout title="Email Verified" subtitle="Your email has been successfully verified">
      <Card>
        <CardContent className="pt-6 text-center">
          <div className="w-16 h-16 rounded-full bg-[#1D9E75]/10 text-[#1D9E75] flex items-center justify-center mx-auto mb-4">
            <Check className="w-8 h-8" />
          </div>
          <p className="font-semibold text-foreground text-lg">Email Verified Successfully</p>
          <p className="text-sm text-muted-foreground mt-2">Your account is now active. You can start using DrugOS.</p>
          <Button className="w-full mt-6 bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'app', section: 'dashboard' })}>
            Continue to DrugOS
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function AcademicVerificationPage() {
  const [email, setEmail] = useState('')
  const [verified, setVerified] = useState(false)

  return (
    <AuthLayout title="Academic Verification" subtitle="Verify your .edu email for free access">
      <Card>
        <CardContent className="pt-6 space-y-4">
          {!verified ? (
            <>
              <div>
                <Label>University Email</Label>
                <Input type="email" placeholder="you@university.edu" value={email} onChange={e => setEmail(e.target.value)} />
                <p className="text-xs text-muted-foreground mt-1">Must be a .edu email address</p>
              </div>
              <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => setVerified(true)}>Verify Academic Status</Button>
            </>
          ) : (
            <div className="text-center py-4">
              <div className="w-14 h-14 rounded-full bg-[#1D9E75]/10 text-[#1D9E75] flex items-center justify-center mx-auto mb-4">
                <GraduationCap className="w-7 h-7" />
              </div>
              <p className="font-semibold text-foreground">Academic Status Verified</p>
              <p className="text-sm text-muted-foreground mt-1">You now have access to the Free Academic plan.</p>
            </div>
          )}
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function OrgSelectionPage() {
  const { navigate } = useRouter()
  const [selected, setSelected] = useState<string | null>(null)
  const orgs = [
    { name: 'DrugOS Corp', plan: 'Professional', members: '18 members' },
    { name: 'University Lab', plan: 'Academic', members: '6 members' },
    { name: 'Personal', plan: 'Free', members: '1 member' },
  ]

  return (
    <AuthLayout title="Select Organization" subtitle="Choose which organization to access">
      <Card>
        <CardContent className="pt-6 space-y-3">
          {orgs.map(org => (
            <button
              key={org.name}
              onClick={() => setSelected(org.name)}
              className={cn(
                'w-full text-left px-4 py-3 rounded-lg border transition-colors flex items-center justify-between',
                selected === org.name ? 'border-[#5B4FCF] bg-[#5B4FCF]/5' : 'border-border hover:bg-accent'
              )}
            >
              <div>
                <span className="font-medium text-foreground">{org.name}</span>
                <p className="text-xs text-muted-foreground">{org.members}</p>
              </div>
              <Badge variant="secondary">{org.plan}</Badge>
            </button>
          ))}
          <Button className="w-full mt-4 bg-[#5B4FCF] hover:bg-[#4B3FBF]" disabled={!selected} onClick={() => navigate({ page: 'app', section: 'dashboard' })}>
            Continue
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function OnboardingWelcomePage() {
  const { navigate } = useRouter()
  const steps = [
    { icon: <User className="w-5 h-5" />, title: 'Select your role', desc: 'Tell us how you plan to use DrugOS' },
    { icon: <Building className="w-5 h-5" />, title: 'Set up your workspace', desc: 'Configure your research environment' },
    { icon: <Users className="w-5 h-5" />, title: 'Invite team members', desc: 'Collaborate with your research team' },
  ]

  return (
    <AuthLayout title="Welcome to DrugOS!" subtitle="Let's get you set up in a few steps">
      <Card>
        <CardContent className="pt-6 space-y-4">
          {steps.map((step, i) => (
            <div key={step.title} className="flex items-center gap-4 p-4 bg-accent rounded-xl">
              <div className="w-10 h-10 rounded-full bg-[#5B4FCF] text-white font-bold text-sm flex items-center justify-center shrink-0">
                {i + 1}
              </div>
              <div>
                <p className="font-medium text-foreground">{step.title}</p>
                <p className="text-sm text-muted-foreground">{step.desc}</p>
              </div>
            </div>
          ))}
          <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'onboarding-role' })}>
            Get Started
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function OnboardingRolePage() {
  const { navigate } = useRouter()
  const [selected, setSelected] = useState<string | null>(null)
  const roles = [
    { icon: <Microscope className="w-5 h-5" />, name: 'Researcher', desc: 'Academic or industry researcher' },
    { icon: <BarChart3 className="w-5 h-5" />, name: 'Data Scientist', desc: 'ML & data analysis' },
    { icon: <Award className="w-5 h-5" />, name: 'PI / Lab Head', desc: 'Principal Investigator' },
    { icon: <Shield className="w-5 h-5" />, name: 'Admin', desc: 'IT & platform management' },
    { icon: <Briefcase className="w-5 h-5" />, name: 'Business Dev', desc: 'Partnerships & licensing' },
    { icon: <Code className="w-5 h-5" />, name: 'Developer', desc: 'API integration & tools' },
  ]

  return (
    <AuthLayout title="What best describes your role?" subtitle="This helps us personalize your experience">
      <Card>
        <CardContent className="pt-6">
          <div className="grid grid-cols-2 gap-3 mb-6">
            {roles.map(role => (
              <button
                key={role.name}
                onClick={() => setSelected(role.name)}
                className={cn(
                  'p-4 rounded-xl border text-left transition-colors',
                  selected === role.name ? 'border-[#5B4FCF] bg-[#5B4FCF]/5' : 'border-border hover:bg-accent'
                )}
              >
                <div className={cn(
                  'w-8 h-8 rounded-lg flex items-center justify-center mb-2',
                  selected === role.name ? 'bg-[#5B4FCF] text-white' : 'bg-accent text-muted-foreground'
                )}>
                  {role.icon}
                </div>
                <p className="font-medium text-foreground text-sm">{role.name}</p>
                <p className="text-xs text-muted-foreground">{role.desc}</p>
              </button>
            ))}
          </div>
          <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" disabled={!selected} onClick={() => navigate({ page: 'onboarding-workspace' })}>
            Continue
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function OnboardingWorkspacePage() {
  const { navigate } = useRouter()
  const [workspaceName, setWorkspaceName] = useState('')
  const [orgName, setOrgName] = useState('')

  return (
    <AuthLayout title="Set up your workspace" subtitle="Name your research workspace and organization">
      <Card>
        <CardContent className="pt-6 space-y-4">
          <div>
            <Label>Workspace Name</Label>
            <Input placeholder="My Research Lab" value={workspaceName} onChange={e => setWorkspaceName(e.target.value)} />
          </div>
          <div>
            <Label>Organization Name</Label>
            <Input placeholder="University or Company" value={orgName} onChange={e => setOrgName(e.target.value)} />
          </div>
          <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'onboarding-invite' })}>
            Create Workspace
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function OnboardingInvitePage() {
  const { navigate } = useRouter()
  const [emails, setEmails] = useState([''])
  const [currentEmail, setCurrentEmail] = useState('')

  const addEmail = () => {
    if (currentEmail && currentEmail.includes('@')) {
      setEmails([...emails, currentEmail])
      setCurrentEmail('')
    }
  }

  return (
    <AuthLayout title="Invite your team" subtitle="Add team members to your workspace">
      <Card>
        <CardContent className="pt-6 space-y-4">
          <div>
            <Label>Email Address</Label>
            <div className="flex gap-2">
              <Input placeholder="colleague@university.edu" value={currentEmail} onChange={e => setCurrentEmail(e.target.value)} onKeyDown={e => e.key === 'Enter' && addEmail()} />
              <Button variant="outline" onClick={addEmail}><Plus className="w-4 h-4" /></Button>
            </div>
          </div>
          {emails.filter(e => e).length > 0 && (
            <div className="space-y-2">
              {emails.filter(e => e).map((email, i) => (
                <div key={i} className="flex items-center justify-between px-3 py-2 bg-accent rounded-lg">
                  <span className="text-sm text-foreground">{email}</span>
                  <button onClick={() => setEmails(emails.filter((_, idx) => idx !== i))} className="text-muted-foreground hover:text-foreground">
                    <X className="w-4 h-4" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <Button className="w-full bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'app', section: 'dashboard' })}>
            Send Invites
          </Button>
          <Button variant="ghost" className="w-full text-muted-foreground" onClick={() => navigate({ page: 'app', section: 'dashboard' })}>
            Skip for now
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function AdminApprovalPage() {
  return (
    <AuthLayout title="Approval Pending" subtitle="Your account requires admin approval">
      <Card>
        <CardContent className="pt-6 text-center">
          <div className="w-16 h-16 rounded-full bg-[#D4853A]/10 text-[#D4853A] flex items-center justify-center mx-auto mb-4">
            <AlertTriangle className="w-8 h-8" />
          </div>
          <p className="font-semibold text-foreground text-lg">Awaiting Admin Approval</p>
          <p className="text-sm text-muted-foreground mt-2">
            Your organization requires admin approval for new accounts. You&apos;ll receive an email once your account is approved.
          </p>
          <div className="mt-6 p-4 bg-accent rounded-xl text-left">
            <p className="text-sm text-muted-foreground">
              <span className="font-medium text-foreground">Typical wait time:</span> 1-2 business days
            </p>
            <p className="text-sm text-muted-foreground mt-1">
              <span className="font-medium text-foreground">Contact:</span> admin@yourorg.com
            </p>
          </div>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function AccountLockedPage() {
  const { navigate } = useRouter()
  return (
    <AuthLayout title="Account Locked" subtitle="Too many failed login attempts">
      <Card>
        <CardContent className="pt-6 text-center">
          <div className="w-16 h-16 rounded-full bg-[#C0392B]/10 text-[#C0392B] flex items-center justify-center mx-auto mb-4">
            <Lock className="w-8 h-8" />
          </div>
          <p className="font-semibold text-foreground text-lg">Account Locked</p>
          <p className="text-sm text-muted-foreground mt-2">
            Your account has been locked due to too many failed login attempts. Please try again after 30 minutes or contact your administrator.
          </p>
          <Button variant="outline" className="w-full mt-6" onClick={() => navigate({ page: 'forgot-password' })}>
            Reset Password
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

// =====================================================================
// APP SHELL (Authenticated Layout)
// =====================================================================

const sidebarNavGroups = [
  {
    label: 'Overview',
    items: [
      { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
      { id: 'pipeline', label: 'Pipeline', icon: GitBranch },
      { id: 'analytics', label: 'Analytics', icon: BarChart3 },
    ]
  },
  {
    label: 'Research',
    items: [
      { id: 'search', label: 'Disease Search', icon: Search },
      { id: 'knowledge-graph', label: 'Knowledge Graph', icon: Network },
      { id: 'clinical-trials', label: 'Clinical Trials', icon: FlaskConical },
      { id: 'safety', label: 'Safety', icon: Shield },
    ]
  },
  {
    label: 'Evidence',
    items: [
      { id: 'evidence-builder', label: 'Evidence Builder', icon: FileText },
      { id: 'reports', label: 'Reports', icon: FileText },
      { id: 'saved-queries', label: 'Saved Queries', icon: Bookmark },
      { id: 'shortlists', label: 'Shortlists', icon: Star },
    ]
  },
  {
    label: 'Team',
    items: [
      { id: 'team', label: 'Team Members', icon: Users },
      { id: 'projects', label: 'Projects', icon: FolderKanban },
      { id: 'shared-queries', label: 'Shared Queries', icon: Share2 },
      { id: 'annotations', label: 'Annotations', icon: MessageSquare },
    ]
  },
  {
    label: 'Data',
    items: [
      { id: 'data-sources', label: 'Data Sources', icon: Database },
      { id: 'graph-stats', label: 'Graph Statistics', icon: BarChart3 },
      { id: 'quality', label: 'Quality', icon: CheckCircle2 },
    ]
  },
  {
    label: 'Billing',
    items: [
      { id: 'subscription', label: 'Subscription', icon: CreditCard },
      { id: 'usage', label: 'Usage', icon: Activity },
      { id: 'deals', label: 'Deals', icon: TrendingUp },
      { id: 'invoices', label: 'Invoices', icon: FileText },
    ]
  },
  {
    label: 'Admin',
    items: [
      { id: 'users', label: 'Users', icon: Users },
      { id: 'roles', label: 'Roles', icon: Shield },
      { id: 'sso', label: 'SSO', icon: Key },
      { id: 'audit-logs', label: 'Audit Logs', icon: FileText },
      { id: 'feature-flags', label: 'Feature Flags', icon: Switch },
    ]
  },
  {
    label: 'Developer',
    items: [
      { id: 'api-docs', label: 'API Docs', icon: BookOpen },
      { id: 'api-keys', label: 'API Keys', icon: Key },
      { id: 'playground', label: 'Playground', icon: Code },
      { id: 'webhooks', label: 'Webhooks', icon: GitFork },
    ]
  },
  {
    label: 'Settings',
    items: [
      { id: 'profile', label: 'Profile', icon: User },
      { id: 'security', label: 'Security', icon: Lock },
      { id: 'notifications', label: 'Notifications', icon: Bell },
      { id: 'preferences', label: 'Preferences', icon: Settings },
    ]
  },
  {
    label: 'Legal',
    items: [
      { id: 'privacy', label: 'Privacy Policy', icon: Eye },
      { id: 'terms', label: 'Terms of Service', icon: Scale },
      { id: 'compliance', label: 'Compliance', icon: ShieldCheck },
    ]
  },
  {
    label: 'Support',
    items: [
      { id: 'help-center', label: 'Help Center', icon: HelpCircle },
      { id: 'tickets', label: 'Support Tickets', icon: FileText },
      { id: 'system-status', label: 'System Status', icon: Activity },
    ]
  },
  {
    label: 'Investor',
    items: [
      { id: 'investor-dashboard', label: 'Dashboard', icon: TrendingUp },
      { id: 'cap-table', label: 'Cap Table', icon: BarChart3 },
    ]
  },
  {
    label: 'More',
    items: [
      { id: 'changelog', label: 'Changelog', icon: GitCommit },
      { id: 'roadmap', label: 'Roadmap', icon: Target },
      { id: 'feedback', label: 'Feedback', icon: MessageSquare },
    ]
  },
]

function AppShell({ children, section }: { children: React.ReactNode; section: string }) {
  const { navigate } = useRouter()
  const { user, loading, logout } = useAuth()
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [expandedGroups, setExpandedGroups] = useState<string[]>(['Overview', 'Research', 'Evidence', 'Team', 'Billing', 'Admin', 'Developer', 'Settings'])
  const [showNotifs, setShowNotifs] = useState(false)
  const [headerSearch, setHeaderSearch] = useState('')

  const unreadNotifs = notifData.filter(n => !n.read).length
  const toggleGroup = (label: string) => {
    setExpandedGroups(prev => prev.includes(label) ? prev.filter(g => g !== label) : [...prev, label])
  }

  // Auth gate: while the initial /api/auth/me check is in flight, show a
  // minimal loading state. If it resolves with no user, redirect to login.
  // This is the SINGLE enforcement point that prevents unauthenticated users
  // from seeing the app — even if they manually navigate to /app/...
  React.useEffect(() => {
    if (!loading && !user) {
      navigate({ page: 'login' })
    }
  }, [loading, user, navigate])

  if (loading || !user) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#F8F8FA]">
        <div className="flex flex-col items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#5B4FCF] to-[#7B6FEF] flex items-center justify-center text-white font-bold animate-pulse">
            D
          </div>
          <p className="text-sm text-muted-foreground">Loading DrugOS…</p>
        </div>
      </div>
    )
  }

  // Derive display name + initials from the real signed-in user.
  const displayName = user.name || user.email.split('@')[0]
  const initials = displayName
    .split(/\s+/)
    .map(p => p[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()
  const handleSignOut = async () => {
    await logout()
    navigate({ page: 'landing' })
  }

  // Get current section label
  const currentLabel = sidebarNavGroups.flatMap(g => g.items).find(i => i.id === section)?.label || section

  const sidebarContent = (
    <div className="flex flex-col h-full">
      <div className="h-14 flex items-center gap-2.5 px-4 border-b border-border shrink-0">
        <button onClick={() => navigate({ page: 'landing' })} className="flex items-center gap-2.5 hover:opacity-80 transition-opacity">
          <DrugOSLogo size="sm" />
          {sidebarOpen && <span className="font-bold text-foreground">DrugOS</span>}
        </button>
      </div>

      {sidebarOpen && (
        <div className="px-3 pt-3">
          <div className="relative">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              value={headerSearch}
              onChange={e => setHeaderSearch(e.target.value)}
              placeholder="Search..."
              className="w-full pl-9 pr-3 py-1.5 text-sm border border-border rounded-lg bg-accent focus:outline-none focus:ring-1 focus:ring-primary/30"
            />
          </div>
        </div>
      )}

      <ScrollArea className="flex-1 py-2">
        <div className="space-y-0.5 px-2">
          {sidebarNavGroups.map(group => {
            const isExpanded = expandedGroups.includes(group.label)
            return (
              <div key={group.label}>
                <button
                  onClick={() => toggleGroup(group.label)}
                  className="w-full flex items-center gap-2 px-2 py-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider hover:text-foreground transition-colors"
                >
                  {sidebarOpen && <span className="flex-1 text-left">{group.label}</span>}
                  {sidebarOpen && (isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />)}
                </button>
                {isExpanded && sidebarOpen && (
                  <div className="space-y-0.5 pb-1">
                    {group.items.map(item => {
                      const Icon = item.icon
                      const isActive = section === item.id
                      return (
                        <button
                          key={item.id}
                          onClick={() => { navigate({ page: 'app', section: item.id }); setMobileOpen(false) }}
                          className={cn(
                            'w-full flex items-center gap-2.5 px-3 py-1.5 text-sm rounded-md transition-colors',
                            isActive ? 'bg-[#5B4FCF]/10 text-[#5B4FCF] font-medium' : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                          )}
                        >
                          <Icon className="w-4 h-4 shrink-0" />
                          <span className="truncate">{item.label}</span>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </ScrollArea>

      <div className="border-t border-border p-3">
        <div className="text-[10px] text-muted-foreground text-center">DrugOS v2.1.0 · © 2026</div>
      </div>
    </div>
  )

  return (
    <div className="min-h-screen flex bg-[#F8F8FA]">
      {/* Mobile overlay */}
      {mobileOpen && <div className="fixed inset-0 bg-black/40 z-40 lg:hidden" onClick={() => setMobileOpen(false)} />}

      {/* Desktop Sidebar */}
      <aside className={cn(
        'hidden lg:flex flex-col border-r border-border bg-card transition-all duration-200 shrink-0',
        sidebarOpen ? 'w-64' : 'w-16'
      )}>
        {sidebarContent}
      </aside>

      {/* Mobile Sidebar */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-72 p-0">
          <SheetTitle className="sr-only">Navigation Menu</SheetTitle>
          {sidebarContent}
        </SheetContent>
      </Sheet>

      {/* Main Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <header className="sticky top-0 z-30 h-14 border-b border-border bg-card/95 backdrop-blur-sm flex items-center px-4 gap-3">
          <Button variant="ghost" size="sm" className="lg:hidden h-8 w-8 p-0" onClick={() => setMobileOpen(true)}>
            <Menu className="h-5 w-5" />
          </Button>
          <Button variant="ghost" size="sm" className="hidden lg:flex h-8 w-8 p-0" onClick={() => setSidebarOpen(!sidebarOpen)}>
            <Menu className="h-4 w-4" />
          </Button>

          <Breadcrumb className="hidden sm:flex">
            <BreadcrumbList>
              <BreadcrumbItem>
                <BreadcrumbLink onClick={() => navigate({ page: 'app', section: 'dashboard' })} className="cursor-pointer">DrugOS</BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator />
              <BreadcrumbItem>
                <BreadcrumbPage>{currentLabel}</BreadcrumbPage>
              </BreadcrumbItem>
            </BreadcrumbList>
          </Breadcrumb>

          <div className="flex-1" />

          <div className="flex items-center gap-2">
            {/* Search */}
            <div className="hidden md:flex items-center relative">
              <Search className="w-4 h-4 absolute left-3 text-muted-foreground" />
              <Input placeholder="Search diseases..." className="pl-9 w-56 h-8 text-sm" />
            </div>

            {/* Notifications */}
            <DropdownMenu open={showNotifs} onOpenChange={setShowNotifs}>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="h-8 w-8 p-0 relative">
                  <Bell className="h-4 w-4" />
                  {unreadNotifs > 0 && (
                    <span className="absolute -top-0.5 -right-0.5 h-4 w-4 rounded-full bg-[#C0392B] text-white text-[10px] font-bold flex items-center justify-center">
                      {unreadNotifs}
                    </span>
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-80">
                <DropdownMenuLabel className="flex items-center justify-between">
                  Notifications
                  <Badge variant="secondary" className="text-[10px]">{unreadNotifs} new</Badge>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                {notifData.slice(0, 5).map(n => (
                  <DropdownMenuItem key={n.id} className="flex flex-col items-start gap-1 p-3 cursor-pointer">
                    <div className="flex items-center gap-2 w-full">
                      <span className={cn(
                        'h-2 w-2 rounded-full shrink-0',
                        n.type === 'success' && 'bg-[#1D9E75]',
                        n.type === 'warning' && 'bg-[#D4853A]',
                        n.type === 'error' && 'bg-[#C0392B]',
                        n.type === 'info' && 'bg-[#5B4FCF]'
                      )} />
                      <span className="text-sm font-medium truncate">{n.title}</span>
                      {!n.read && <Badge className="ml-auto text-[9px] h-4">New</Badge>}
                    </div>
                    <span className="text-xs text-muted-foreground line-clamp-1">{n.message}</span>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>

            {/* User Menu */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="h-8 gap-2 px-2">
                  <Avatar className="h-6 w-6">
                    <AvatarFallback className="bg-[#5B4FCF] text-white text-[10px]">{initials}</AvatarFallback>
                  </Avatar>
                  <span className="hidden sm:inline text-sm font-medium">{displayName}</span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <DropdownMenuLabel>
                  <div className="flex flex-col">
                    <span>{displayName}</span>
                    <span className="text-xs font-normal text-muted-foreground">{user.email}</span>
                  </div>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => navigate({ page: 'app', section: 'profile' })}>
                  <User className="mr-2 h-4 w-4" /> Profile
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => navigate({ page: 'app', section: 'settings' })}>
                  <Settings className="mr-2 h-4 w-4" /> Settings
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={handleSignOut}>
                  <LogOut className="mr-2 h-4 w-4" /> Sign Out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>

        {/* Page Content */}
        <main className="flex-1 overflow-auto p-4 md:p-6">
          {children}
        </main>
      </div>
    </div>
  )
}

// =====================================================================
// APP SECTION PAGES
// =====================================================================

function AppDashboard() {
  const { navigate } = useRouter()

  return (
    <div>
      <SectionHeading
        title="Dashboard"
        subtitle="Welcome back, Dr. Sarah Chen"
        action={<Button className="bg-[#5B4FCF] hover:bg-[#4B3FBF]" onClick={() => navigate({ page: 'app', section: 'search' })}><Search className="w-4 h-4 mr-1" /> New Search</Button>}
      />

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {[
          { title: 'Queries Today', value: '8/10', subtitle: 'Free tier limit', icon: <Search className="w-5 h-5" /> },
          { title: 'Saved Candidates', value: '24', trend: '+3 this week', icon: <Star className="w-5 h-5" /> },
          { title: 'Reports', value: '12', trend: '+2 this week', icon: <Download className="w-5 h-5" /> },
          { title: 'API Calls', value: '1,247', subtitle: 'of 50K limit', icon: <Code className="w-5 h-5" /> },
        ].map(stat => (
          <Card key={stat.title} className="hover:shadow-md transition-shadow">
            <CardContent className="pt-6">
              <div className="flex items-start justify-between">
                <div>
                  <p className="text-sm text-muted-foreground">{stat.title}</p>
                  <p className="text-2xl font-bold text-foreground mt-1">{stat.value}</p>
                  {stat.subtitle && <p className="text-xs text-muted-foreground mt-1">{stat.subtitle}</p>}
                  {stat.trend && <p className="text-xs text-[#1D9E75] mt-1">{stat.trend}</p>}
                </div>
                <div className="w-10 h-10 rounded-lg bg-[#5B4FCF]/10 text-[#5B4FCF] flex items-center justify-center">{stat.icon}</div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Queries */}
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Recent Queries</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {recentQueries.map(q => (
                <div key={q.id} className="flex items-center justify-between py-2 border-b border-border last:border-0">
                  <div>
                    <p className="font-medium text-foreground text-sm">{q.disease}</p>
                    <p className="text-xs text-muted-foreground">{q.date}</p>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-xs text-muted-foreground">{q.candidates} candidates</span>
                    <div className="w-16"><ScoreBar score={q.topScore} size="sm" /></div>
                    <span className="text-sm font-bold text-foreground">{q.topScore}</span>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        {/* Usage */}
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Usage This Period</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {[
              { label: 'Queries', value: usageMetrics.queries.used, max: usageMetrics.queries.limit },
              { label: 'API Calls', value: usageMetrics.apiCalls.used, max: usageMetrics.apiCalls.limit },
              { label: 'Reports', value: usageMetrics.reports.used, max: usageMetrics.reports.limit },
            ].map(item => (
              <div key={item.label}>
                <div className="flex justify-between text-sm mb-1">
                  <span className="text-muted-foreground">{item.label}</span>
                  <span className="text-foreground font-medium">{item.value.toLocaleString()} / {item.max.toLocaleString()}</span>
                </div>
                <Progress value={(item.value / item.max) * 100} className="h-2" />
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function AppSearchPage() {
  const { navigate } = useRouter()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<typeof diseases>([])

  const handleSearch = () => {
    if (query.length >= 2) {
      setResults(diseases.filter(d => d.name.toLowerCase().includes(query.toLowerCase()) || d.therapeuticArea.toLowerCase().includes(query.toLowerCase())))
    }
  }

  return (
    <div>
      <SectionHeading title="Disease Search" subtitle="Search for a disease to find drug repurposing candidates" />
      <div className="max-w-3xl mx-auto text-center py-8">
        <div className="relative mb-8">
          <Search className="w-5 h-5 absolute left-4 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleSearch()}
            placeholder="Search for a disease, condition, or ICD code..."
            className="w-full pl-12 pr-32 py-4 text-lg border border-border rounded-2xl focus:outline-none focus:ring-2 focus:ring-[#5B4FCF]/20 focus:border-[#5B4FCF] shadow-lg shadow-slate-200/50 bg-white"
          />
          <Button className="absolute right-2 top-2 bottom-2 px-6 bg-[#5B4FCF] hover:bg-[#4B3FBF] rounded-xl" onClick={handleSearch}>Search</Button>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-left">
          <Card>
            <CardContent className="pt-4">
              <h4 className="text-sm font-medium text-muted-foreground mb-3">Recent Queries</h4>
              {recentQueries.slice(0, 3).map(q => (
                <button key={q.id} onClick={() => { setQuery(q.disease); handleSearch() }} className="block text-sm py-1.5 text-foreground hover:text-[#5B4FCF] cursor-pointer">{q.disease}</button>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <h4 className="text-sm font-medium text-muted-foreground mb-3">Trending</h4>
              {trendingDiseases.slice(0, 3).map(d => (
                <button key={d.name} onClick={() => { setQuery(d.name); handleSearch() }} className="block text-sm py-1.5 text-foreground hover:text-[#5B4FCF] cursor-pointer">{d.name}</button>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <h4 className="text-sm font-medium text-muted-foreground mb-3">Quick Start</h4>
              {diseases.slice(0, 3).map(d => (
                <button key={d.id} onClick={() => { setQuery(d.name); handleSearch() }} className="block text-sm py-1.5 text-foreground hover:text-[#5B4FCF] cursor-pointer">{d.name}</button>
              ))}
            </CardContent>
          </Card>
        </div>

        {/* Search Results */}
        {results.length > 0 && (
          <div className="mt-8 text-left">
            <Card>
              <CardHeader>
                <CardTitle className="text-lg">Results ({results.length})</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-3">
                  {results.map(d => (
                    <button
                      key={d.id}
                      onClick={() => navigate({ page: 'app', section: 'search', sub: 'results', id: d.id })}
                      className="w-full flex items-center justify-between p-4 rounded-xl border border-border hover:bg-accent transition-colors text-left"
                    >
                      <div>
                        <p className="font-medium text-foreground">{d.name}</p>
                        <div className="flex items-center gap-2 mt-1">
                          <Badge variant="outline" className="text-xs">{d.icdCode}</Badge>
                          <Badge variant="secondary" className="text-xs">{d.therapeuticArea}</Badge>
                        </div>
                      </div>
                      <ArrowRight className="w-4 h-4 text-muted-foreground" />
                    </button>
                  ))}
                </div>
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </div>
  )
}

function AppSearchResultsPage({ diseaseId }: { diseaseId?: string }) {
  const disease = diseases.find(d => d.id === diseaseId) || diseases[0]
  const candidates = drugCandidates.filter(d => d.diseaseId === disease.id)

  return (
    <div>
      <SectionHeading
        title={`${disease.name} — Candidates`}
        subtitle={`${candidates.length} drug repurposing candidates found`}
        action={<Button variant="outline"><Download className="w-4 h-4 mr-1" />Export CSV</Button>}
      />

      <Card>
        <CardContent className="pt-6">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  {['#', 'Drug Name', 'Composite', 'Safety', 'Mechanism', 'Phase', 'IP'].map(h => (
                    <th key={h} className="text-left py-3 px-3 font-medium text-muted-foreground text-xs uppercase tracking-wider">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {candidates.map((d, i) => (
                  <tr key={d.id} className="border-b border-border/50 hover:bg-accent/50 transition-colors">
                    <td className="py-3 px-3 font-bold text-muted-foreground">{i + 1}</td>
                    <td className="py-3 px-3">
                      <div>
                        <span className="font-semibold text-foreground">{d.drugName}</span>
                        <br />
                        <span className="text-xs text-muted-foreground">{d.brandNames.join(', ')}</span>
                      </div>
                    </td>
                    <td className="py-3 px-3">
                      <div className="w-24">
                        <div className="flex items-center justify-between text-sm mb-1"><span className="font-bold">{d.compositeScore}</span></div>
                        <ScoreBar score={d.compositeScore} size="sm" />
                      </div>
                    </td>
                    <td className="py-3 px-3"><SafetyBadge tier={d.safetyTier} /></td>
                    <td className="py-3 px-3 max-w-[200px]"><span className="text-xs text-muted-foreground line-clamp-2">{d.mechanism}</span></td>
                    <td className="py-3 px-3"><span className="text-xs font-medium text-foreground">{d.clinicalPhase}</span></td>
                    <td className="py-3 px-3"><span className="text-xs text-muted-foreground">{d.ipStatus}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function AppPlaceholderSection({ section }: { section: string }) {
  const label = sidebarNavGroups.flatMap(g => g.items).find(i => i.id === section)?.label || section
  const Icon = sidebarNavGroups.flatMap(g => g.items).find(i => i.id === section)?.icon || LayoutDashboard

  return (
    <div>
      <SectionHeading title={label} subtitle={`This is the ${label} section of DrugOS`} />
      <Card>
        <CardContent className="pt-6 text-center py-16">
          <div className="w-16 h-16 rounded-2xl bg-[#5B4FCF]/10 text-[#5B4FCF] flex items-center justify-center mx-auto mb-4">
            <Icon className="w-8 h-8" />
          </div>
          <h3 className="text-lg font-semibold text-foreground">{label}</h3>
          <p className="text-sm text-muted-foreground mt-2 max-w-md mx-auto">
            This section is under development. Check back soon for full {label.toLowerCase()} functionality.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}

// Bridge component to render core screens from core-screens.tsx
function CoreScreenBridge({ section, sub, id }: { section: string; sub?: string; id?: string }) {
  const { navigate: routerNavigate, route: routerRoute } = useRouter()

  // Map the app-router's navigate to the core-screens nav format
  const navContextValue = useMemo(() => ({
    navigate: (r: { page: string; section?: string; sub?: string; id?: string }) => {
      routerNavigate({ page: 'app', section: r.section || r.page, sub: r.sub, id: r.id })
    },
    currentRoute: { page: 'app', section: section, sub: sub, id: id },
  }), [routerNavigate, section, sub, id])

  const ScreenComponent = coreScreens[section] || allScreens[section]

  if (!ScreenComponent) {
    return <AppPlaceholderSection section={section} />
  }

  return (
    <DrugOSNavContext.Provider value={navContextValue}>
      <ScreenComponent />
    </DrugOSNavContext.Provider>
  )
}

function AppSectionRenderer({ section, sub, id }: { section: string; sub?: string; id?: string }) {
  if (section === 'dashboard') return <AppDashboard />
  // Delegate to core screens from core-screens.tsx
  return <CoreScreenBridge section={section} sub={sub} id={id} />
}

// =====================================================================
// MAIN APP ROUTER COMPONENT
// =====================================================================

export default function DrugOSApp() {
  const [route, setRoute] = useState<Route>({ page: 'landing' })

  const routerContext = useMemo(() => ({
    route,
    navigate: (r: Route) => setRoute(r),
  }), [route])

  const renderPage = () => {
    // Public pages (with PublicLayout)
    const publicPages: Record<string, React.ReactNode> = {
      'landing': <LandingPage />,
      'pricing': <PricingPage />,
      'about': <AboutPage />,
      'security': <SecurityPage />,
      'status': <StatusPage />,
      'blog': <BlogPage />,
      'contact': <ContactPage />,
      'careers': <CareersPage />,
      'case-studies': <CaseStudiesPage />,
    }

    if (route.page === 'features') {
      return <PublicLayout><FeaturePage slug={route.slug} /></PublicLayout>
    }

    if (publicPages[route.page]) {
      return <PublicLayout>{publicPages[route.page]}</PublicLayout>
    }

    // Auth pages
    const authPages: Record<string, React.ReactNode> = {
      'login': <LoginPage />,
      'register': <RegisterPage />,
      'forgot-password': <ForgotPasswordPage />,
      'reset-password': <ResetPasswordPage />,
      'mfa-challenge': <MFAChallengePage />,
      'email-verification': <EmailVerificationPage />,
      'academic-verification': <AcademicVerificationPage />,
      'org-selection': <OrgSelectionPage />,
      'onboarding-welcome': <OnboardingWelcomePage />,
      'onboarding-role': <OnboardingRolePage />,
      'onboarding-workspace': <OnboardingWorkspacePage />,
      'onboarding-invite': <OnboardingInvitePage />,
      'admin-approval': <AdminApprovalPage />,
      'account-locked': <AccountLockedPage />,
    }

    if (authPages[route.page]) {
      return authPages[route.page]
    }

    // App pages (with AppShell)
    if (route.page === 'app') {
      return (
        <AppShell section={route.section}>
          <AppSectionRenderer section={route.section} sub={route.sub} id={route.id} />
        </AppShell>
      )
    }

    // Fallback
    return <PublicLayout><LandingPage /></PublicLayout>
  }

  return (
    <AuthProvider>
      <RouterContext.Provider value={routerContext}>
        <AnimatePresence mode="wait">
          <motion.div
            key={route.page === 'app' ? `app-${route.section}` : route.page === 'features' ? `features-${route.slug}` : route.page}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
          >
            {renderPage()}
          </motion.div>
        </AnimatePresence>
      </RouterContext.Provider>
    </AuthProvider>
  )
}
