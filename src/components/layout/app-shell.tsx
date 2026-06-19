'use client';

import { useState, useCallback } from 'react';
import {
  LayoutDashboard,
  Search,
  Pill,
  Share2,
  FlaskConical,
  ScrollText,
  Package,
  FileBarChart,
  Users,
  Database,
  CreditCard,
  UserCog,
  Scale,
  Code,
  Settings,
  CircleHelp,
  TrendingUp,
  ChevronDown,
  ChevronRight,
  Bell,
  Menu,
  LogOut,
  User,
  Sun,
  ShieldCheck,
  AlertTriangle,
  Clock,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Sheet,
  SheetContent,
  SheetTrigger,
  SheetTitle,
} from '@/components/ui/sheet';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb';
import {
  sidebarCategories,
  type ScreenCategory,
} from '@/lib/screens';
import { notifications } from '@/lib/mock-data';
import { cn } from '@/lib/utils';

// ---- Icon Map ----

const iconMap: Record<string, React.ComponentType<{ className?: string }>> = {
  LayoutDashboard,
  Search,
  Pill,
  Share2,
  FlaskConical,
  ScrollText,
  Package,
  FileBarChart,
  Users,
  Database,
  CreditCard,
  UserCog,
  Scale,
  Code,
  Settings,
  CircleHelp,
  TrendingUp,
  Network: Share2,
  ShieldCheck: Package,
  Route: Share2,
  AlertTriangle: Package,
  Stethoscope: FlaskConical,
  Landmark: ScrollText,
  ClipboardCheck: Package,
  Filter: Search,
  Bookmark: FileBarChart,
  Columns3: Package,
  AlertOctagon: AlertTriangle,
  Atom: Pill,
  PieChart: FileBarChart,
  Star: FileBarChart,
  History: FileBarChart,
  Layers: Database,
  FolderOpen: Package,
  Share: Share2,
  MessageSquare: CircleHelp,
  MessagesSquare: CircleHelp,
  Paperclip: FileBarChart,
  Lock: ShieldCheck,
  Clock: Clock,
  BellRing: Bell,
  UsersRound: Users,
  LayoutGrid: LayoutDashboard,
  ExternalLink: Share2,
  Globe: TrendingUp,
  Building: Database,
  Eye: Search,
  Merge: Share2,
  CheckCircle: ShieldCheck,
  ArrowDownToLine: Database,
  GitFork: Share2,
  Link2: Share2,
  Sigma: FileBarChart,
  Timer: Clock,
  Shuffle: Share2,
  Receipt: CreditCard,
  FileSignature: ScrollText,
  Wallet: CreditCard,
  Calculator: CreditCard,
  Gauge: FileBarChart,
  Workflow: Share2,
  Flag: FileBarChart,
  Percent: CreditCard,
  FolderLock: Database,
  ShoppingCart: CreditCard,
  UserCog,
  KeyRound: ShieldCheck,
  Fingerprint: Users,
  UserPlus: Users,
  Sitemap: Database,
  Settings2: Settings,
  ToggleRight: Settings,
  MapPin: TrendingUp,
  Globe2: TrendingUp,
  Palette: Settings,
  Plug: Code,
  Mail: CircleHelp,
  BellCog: Bell,
  ShieldAlert: ShieldCheck,
  Key: ShieldCheck,
  Scroll: ScrollText,
  ScanSearch: Search,
  PenTool: Settings,
  FlaskRound: FlaskConical,
  Tag: Settings,
  FileCheck: ScrollText,
  Handshake: Users,
  Hospital: ShieldCheck,
  Cookie: FileBarChart,
  Download: Database,
  Archive: Database,
  ListTree: Database,
  Eraser: Settings,
  GraduationCap: TrendingUp,
  BookOpen: FileBarChart,
  Play: Code,
  Webhook: Code,
  GitCommitHorizontal: FileBarChart,
  Code2: Code,
  Braces: Code,
  Upload: Database,
  Bug: AlertTriangle,
  Brain: TrendingUp,
  ArrowUpFromLine: Database,
  ScrollText,
  User,
  Accessibility: Users,
  Languages: TrendingUp,
  LogOut,
  Smartphone: Settings,
  CircleHelp,
  HelpCircle: CircleHelp,
  Headphones: CircleHelp,
  Ticket: FileBarChart,
  Library: Database,
  GitCommit: FileBarChart,
  Video: TrendingUp,
  MessageCircle: CircleHelp,
  MessageSquarePlus: CircleHelp,
  CheckSquare: ShieldCheck,
  Lightbulb: TrendingUp,
  Map: TrendingUp,
  Sparkles: TrendingUp,
  DollarSign: CreditCard,
  Home: LayoutDashboard,
  Info: CircleHelp,
  Newspaper: FileBarChart,
  Briefcase: TrendingUp,
  BookMarked: FileBarChart,
  FileQuestion: CircleHelp,
  Wrench: Settings,
  ArrowUpCircle: TrendingUp,
  Inbox: Database,
  Loader2: Settings,
  Compass: TrendingUp,
  Megaphone: Bell,
  Printer: FileBarChart,
  RotateCcw: Settings,
  FolderKanban: Package,
  BarChart3: FileBarChart,
  FileText: ScrollText,
  Activity: TrendingUp,
  GitBranch: Share2,
  Target: Search,
  Heart: ShieldCheck,
  LineChart: TrendingUp,
  Shield: ShieldCheck,
  Bell,
  LogIn: User,
  UserCheck: ShieldCheck,
  Hourglass: Clock,
  PartyPopper: TrendingUp,
  Siren: AlertTriangle,
};

// ---- Types ----

interface AppShellProps {
  activeScreen: string;
  onNavigate: (screenId: string) => void;
  children: React.ReactNode;
}

// ---- Sidebar Category ----

function SidebarCategorySection({
  category,
  activeScreen,
  onNavigate,
  defaultOpen = false,
}: {
  category: (typeof sidebarCategories)[number];
  activeScreen: string;
  onNavigate: (id: string) => void;
  defaultOpen?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const IconComponent = iconMap[category.icon] ?? LayoutDashboard;

  return (
    <div>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 w-full px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider hover:text-foreground transition-colors"
      >
        <IconComponent className="h-3.5 w-3.5" />
        <span className="flex-1 text-left">{category.id}</span>
        {isOpen ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
      </button>
      {isOpen && (
        <div className="space-y-0.5 px-1 pb-1">
          {category.items.map((item) => {
            const ItemIcon = iconMap[item.icon] ?? LayoutDashboard;
            const isActive = activeScreen === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onNavigate(item.id)}
                className={cn(
                  'flex items-center gap-2.5 w-full px-3 py-1.5 text-sm rounded-md transition-colors',
                  isActive
                    ? 'bg-primary/10 text-primary font-medium'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                )}
              >
                <ItemIcon className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{item.name}</span>
                <span className="ml-auto text-[10px] text-muted-foreground/60 font-mono">
                  {item.id}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---- Sidebar Content ----

function SidebarContent({
  activeScreen,
  onNavigate,
}: {
  activeScreen: string;
  onNavigate: (id: string) => void;
}) {
  return (
    <div className="flex flex-col h-full">
      {/* Logo */}
      <div className="px-4 py-4 flex items-center gap-3">
        <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-[#5B4FCF] to-[#7B6FEF] flex items-center justify-center text-white font-bold text-lg shadow-lg shadow-[#5B4FCF]/20">
          D
        </div>
        <div>
          <h1 className="text-base font-bold text-foreground leading-tight">DrugOS</h1>
          <p className="text-[10px] text-muted-foreground leading-tight">Drug Repurposing Platform</p>
        </div>
      </div>
      <Separator />

      {/* Navigation */}
      <ScrollArea className="flex-1 px-2 py-2">
        <div className="space-y-1">
          {sidebarCategories.map((cat, idx) => (
            <SidebarCategorySection
              key={cat.id}
              category={cat}
              activeScreen={activeScreen}
              onNavigate={onNavigate}
              defaultOpen={idx <= 1}
            />
          ))}
        </div>
      </ScrollArea>

      {/* Footer */}
      <Separator />
      <div className="p-3">
        <div className="text-[10px] text-muted-foreground text-center">
          DrugOS v2.1.0 · © 2026
        </div>
      </div>
    </div>
  );
}

// ---- Main App Shell ----

export function AppShell({ activeScreen, onNavigate, children }: AppShellProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const unreadCount = notifications.filter((n) => !n.read).length;

  const handleNavigate = useCallback(
    (screenId: string) => {
      onNavigate(screenId);
    },
    [onNavigate]
  );

  // Get active screen info for breadcrumb
  const activeMeta = sidebarCategories
    .flatMap((c) => c.items)
    .find((i) => i.id === activeScreen);

  return (
    <div className="min-h-screen flex bg-background">
      {/* Desktop Sidebar */}
      <aside
        className={cn(
          'hidden lg:flex flex-col border-r border-border bg-card transition-all duration-200',
          sidebarCollapsed ? 'w-0 overflow-hidden' : 'w-64'
        )}
      >
        <SidebarContent activeScreen={activeScreen} onNavigate={handleNavigate} />
      </aside>

      {/* Main Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top Header */}
        <header className="sticky top-0 z-30 h-14 border-b border-border bg-card/95 backdrop-blur-sm flex items-center px-4 gap-3">
          {/* Mobile menu */}
          <Sheet>
            <SheetTrigger asChild>
              <Button variant="ghost" size="sm" className="lg:hidden h-8 w-8 p-0">
                <Menu className="h-5 w-5" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-72 p-0">
              <SheetTitle className="sr-only">Navigation Menu</SheetTitle>
              <SidebarContent activeScreen={activeScreen} onNavigate={handleNavigate} />
            </SheetContent>
          </Sheet>

          {/* Sidebar toggle (desktop) */}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            className="hidden lg:flex h-8 w-8 p-0"
          >
            <Menu className="h-4 w-4" />
          </Button>

          {/* Breadcrumb */}
          <Breadcrumb className="hidden sm:flex">
            <BreadcrumbList>
              <BreadcrumbItem>
                <BreadcrumbLink
                  onClick={() => handleNavigate('DASH-01')}
                  className="cursor-pointer"
                >
                  DrugOS
                </BreadcrumbLink>
              </BreadcrumbItem>
              {activeMeta && (
                <>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    <BreadcrumbLink
                      onClick={() => {
                        const firstInCategory = sidebarCategories.find(
                          (c) => c.id === activeMeta.category
                        )?.items[0]?.id;
                        if (firstInCategory) handleNavigate(firstInCategory);
                      }}
                      className="cursor-pointer"
                    >
                      {activeMeta.category}
                    </BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    <BreadcrumbPage>{activeMeta.name}</BreadcrumbPage>
                  </BreadcrumbItem>
                </>
              )}
            </BreadcrumbList>
          </Breadcrumb>

          {/* Screen ID badge */}
          <Badge variant="outline" className="font-mono text-[10px] hidden sm:flex">
            {activeScreen}
          </Badge>

          <div className="flex-1" />

          {/* Right side: Notifications + User */}
          <div className="flex items-center gap-2">
            {/* Notifications */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="h-8 w-8 p-0 relative">
                  <Bell className="h-4 w-4" />
                  {unreadCount > 0 && (
                    <span className="absolute -top-0.5 -right-0.5 h-4 w-4 rounded-full bg-[#C0392B] text-white text-[10px] font-bold flex items-center justify-center">
                      {unreadCount}
                    </span>
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-80">
                <DropdownMenuLabel className="flex items-center justify-between">
                  Notifications
                  <Badge variant="secondary" className="text-[10px]">
                    {unreadCount} new
                  </Badge>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                {notifications.slice(0, 5).map((notif) => (
                  <DropdownMenuItem
                    key={notif.id}
                    className="flex flex-col items-start gap-1 p-3 cursor-pointer"
                  >
                    <div className="flex items-center gap-2 w-full">
                      <span
                        className={cn(
                          'h-2 w-2 rounded-full shrink-0',
                          notif.type === 'success' && 'bg-[#1D9E75]',
                          notif.type === 'warning' && 'bg-[#D4853A]',
                          notif.type === 'error' && 'bg-[#C0392B]',
                          notif.type === 'info' && 'bg-[#5B4FCF]'
                        )}
                      />
                      <span className="text-sm font-medium truncate">{notif.title}</span>
                      {!notif.read && (
                        <Badge className="ml-auto text-[9px] h-4">New</Badge>
                      )}
                    </div>
                    <span className="text-xs text-muted-foreground line-clamp-1">{notif.message}</span>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>

            {/* User Menu */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="h-8 gap-2 px-2">
                  <Avatar className="h-6 w-6">
                    <AvatarFallback className="bg-primary text-primary-foreground text-[10px]">
                      SC
                    </AvatarFallback>
                  </Avatar>
                  <span className="hidden sm:inline text-sm font-medium">Dr. Sarah Chen</span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <DropdownMenuLabel>
                  <div className="flex flex-col">
                    <span>Dr. Sarah Chen</span>
                    <span className="text-xs font-normal text-muted-foreground">sarah.chen@drugos.io</span>
                  </div>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => handleNavigate('SET-01')}>
                  <User className="mr-2 h-4 w-4" /> Profile
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => handleNavigate('SET-02')}>
                  <ShieldCheck className="mr-2 h-4 w-4" /> Security
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => handleNavigate('SET-04')}>
                  <Sun className="mr-2 h-4 w-4" /> Theme
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem>
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
  );
}
