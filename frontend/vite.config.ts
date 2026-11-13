import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // The gateway is loopback-only; proxying keeps the browser on one origin.
    proxy: { "/v1": "http://127.0.0.1:8000", "/health": "http://127.0.0.1:8000" },
    // The policy page reads backend/policy.yaml directly, so it can never drift from the running rules.
    fs: { allow: [".."] },
  },
});
