import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The mobile client is a standalone project. In development it talks to the
// existing PC admin backend (default http://127.0.0.1:8765) through a proxy so
// the browser stays same-origin and no CORS is needed. Override the target with
// the DIGISELLER_ADMIN_ORIGIN env var when the backend runs elsewhere.
const backend = process.env.DIGISELLER_ADMIN_ORIGIN || "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: backend,
        changeOrigin: true,
      },
    },
  },
});
