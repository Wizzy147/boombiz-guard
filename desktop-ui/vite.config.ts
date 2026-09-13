import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In production the agent serves dist/ itself on 127.0.0.1:7480. During UI
// development Vite proxies /api to the agent (run the agent with GUARD_DEV=1
// so it accepts the dev-server origin).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:7480", changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: false },
});
