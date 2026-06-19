import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { Geist_Mono } from "next/font/google";
import "./globals.css";
import { Toaster } from "@/components/ui/toaster";

const interSans = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "DrugOS — Autonomous Drug Repurposing Platform",
  description: "AI-powered drug repurposing platform for discovering new therapeutic uses of existing drugs. Search diseases, rank candidates, explore knowledge graphs, and build evidence packages.",
  keywords: ["DrugOS", "drug repurposing", "AI", "knowledge graph", "clinical trials", "pharmaceutical"],
  authors: [{ name: "DrugOS Team" }],
  icons: {
    icon: "https://z-cdn.chatglm.cn/z-ai/static/logo.svg",
  },
  openGraph: {
    title: "DrugOS — Drug Repurposing Platform",
    description: "AI-powered drug repurposing for rare and complex diseases",
    siteName: "DrugOS",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${interSans.variable} ${geistMono.variable} antialiased bg-background text-foreground`}
      >
        {children}
        <Toaster />
      </body>
    </html>
  );
}
