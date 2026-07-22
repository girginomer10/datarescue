import { copyFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

function normalizedBasePath(value = "/"): string {
  const withLeadingSlash = value.startsWith("/") ? value : `/${value}`;
  return withLeadingSlash.endsWith("/") ? withLeadingSlash : `${withLeadingSlash}/`;
}

const projectRoot = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  base: normalizedBasePath(process.env.VITE_BASE_PATH),
  publicDir: resolve(projectRoot, "../../demo/replay"),
  plugins: [
    react(),
    {
      name: "github-pages-spa-fallback",
      closeBundle() {
        const indexPath = resolve(projectRoot, "dist/index.html");
        if (existsSync(indexPath)) copyFileSync(indexPath, resolve(projectRoot, "dist/404.html"));
      },
    },
  ],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          "query-vendor": ["@tanstack/react-query"],
          "flow-vendor": ["@xyflow/react"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
});
