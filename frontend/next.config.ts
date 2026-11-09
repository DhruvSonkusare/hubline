import type { NextConfig } from "next";

// Cache Components / partial prefetching are deliberately OFF: every dashboard page is
// per-request, behind a login and live (polling agent status), so a prerendered shell gains
// nothing and would force Suspense around every cookie read. Plain dynamic rendering instead.
const nextConfig: NextConfig = {
  poweredByHeader: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
