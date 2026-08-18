import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served by FastAPI StaticFiles under the /app prefix (see
// src/brains/web/spa.py). `base` makes built asset URLs resolve there.
// `build.outDir` points into the Python package so the wheel ships the
// compiled bundle (pyproject package-data: brains.web -> spa/**/*).
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "../src/brains/web/spa",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      "/v1": "http://127.0.0.1:8080",
    },
  },
});
