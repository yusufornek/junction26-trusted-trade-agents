import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const GATEWAY = "http://127.0.0.1:8000";
// Same paths deploy/Caddyfile sends to the gateway, so dev behaves like production.
const GATEWAY_PATHS = ["/v1", "/health", "/.well-known", "/oauth", "/mcp", "/a2a", "/developers", "/llms.txt", "/docs", "/openapi.json"];

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(GATEWAY_PATHS.map((p) => [p, GATEWAY])),
  },
});
