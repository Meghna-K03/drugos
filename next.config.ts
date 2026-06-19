import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  reactStrictMode: false,
  // Allow preview host to access dev server
  allowedDevOrigins: [
    "preview-chat-51282d95-9c3f-4849-af7f-41b08e01ba46.space-z.ai",
  ],
  // Disable Turbopack — use webpack (more compatible with @next/swc 16.1.3 binary)
  // Turbopack requires the exact-matching swc binary; webpack works with the older one.
};

export default nextConfig;
