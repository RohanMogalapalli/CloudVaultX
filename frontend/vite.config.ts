import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The coordinator API is proxied in dev so the browser only ever talks to
// one origin (matching the nginx setup used in production/compose).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false, // bump to 5174+ when 5173 is reserved (Hyper-V/WSL port exclusion ranges)
    host: true,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
