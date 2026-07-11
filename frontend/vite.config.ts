import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy backend routes so the browser calls same-origin (no CORS). The backend
// runs inside WSL on :8123, reachable from Windows via localhostForwarding.
// Override the target with VITE_BACKEND if you run it elsewhere.
const BACKEND = process.env.VITE_BACKEND ?? "http://127.0.0.1:8123";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/health": BACKEND,
      "/agents": BACKEND,
      "/tasks": BACKEND,
      "/act": BACKEND,
      // SSE: disable buffering so events stream through immediately.
      "/events": { target: BACKEND, changeOrigin: true },
    },
  },
});
