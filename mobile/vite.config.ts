import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The mobile client is a standalone project. In development it talks to the
// existing PC admin backend (default http://127.0.0.1:8765) through a proxy so
// the browser stays same-origin and no CORS is needed. Override the target with
// the DIGISELLER_ADMIN_ORIGIN env var when the backend runs elsewhere.
const backend = process.env.DIGISELLER_ADMIN_ORIGIN || "http://127.0.0.1:8765";

// The mobile client runs on its own port, separate from the PC admin. Both the
// dev server and the production server (mobile/serve.py) proxy /api and /assets
// to the backend, so the app is always same-origin and needs no CORS.
const proxy = {
  "/api": { target: backend, changeOrigin: true },
  // brand logos referenced by relative paths (e.g. /assets/brand-logos/*)
  "/assets": { target: backend, changeOrigin: true },
};

export default defineConfig({
  plugins: [react()],
  build: {
    assetsDir: "static",
  },
  server: {
    host: true,
    port: 5173,
    proxy,
  },
  preview: {
    host: true,
    port: Number(process.env.MOBILE_PORT) || 8080,
    proxy,
  },
});
