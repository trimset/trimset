import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// base "./": the server mounts the build under /ui (ADR 0004), and relative asset paths
// work there or anywhere else. In development, API calls go to compose's port.
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: { proxy: { "/v1": "http://localhost:8800" } },
});
