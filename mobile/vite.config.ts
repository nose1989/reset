import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The mobile client is a standalone project. In development it talks to the
// existing PC admin backend (default http://127.0.0.1:8765) through a proxy so
// the browser stays same-origin and no CORS is needed. Override the target with
// the DIGISELLER_ADMIN_ORIGIN env var when the backend runs elsewhere.
const backend = process.env.DIGISELLER_ADMIN_ORIGIN || "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  // Served under /m by the backend (single-process deployment). All asset URLs
  // and the router are prefixed accordingly.
  base: "/m/",
  // Emit the SPA's own assets under /static so they never collide with the
  // backend's /assets (brand logos) when both are served from one origin.
  build: {
    assetsDir: "static",
  },
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: backend,
        changeOrigin: true,
      },
      // brand logos referenced by relative paths (e.g. /assets/brand-logos/*)
      "/assets": {
        target: backend,
        changeOrigin: true,
      },
    },
  },
});
