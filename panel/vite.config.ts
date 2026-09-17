/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  // 026 (contracts/cockpit-read-model.md §5): assets relativos al documento
  // que los carga, no a "/" -- el MISMO build sirve tal cual en directo
  // (`/`) y empotrado bajo `/ads/` (el `<base href>` que inyecta
  // `composition/app.py::_render_embedded_index_html` resuelve el resto).
  base: "./",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-query": ["@tanstack/react-query"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
    css: false,
  },
});
