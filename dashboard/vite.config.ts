import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// base "./": the server mounts the build under /ui (ADR 0004), and relative asset paths
// work there or anywhere else. In development, API calls go to compose's port. Only the
// demo build (--mode demo) ships demo/public/, the recording it answers from.
export default defineConfig(({ mode }) => ({
  base: "./",
  publicDir: mode === "demo" ? "demo/public" : false,
  plugins: [react()],
  server: { proxy: { "/v1": "http://localhost:8800" } },
}));
