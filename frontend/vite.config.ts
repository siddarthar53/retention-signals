import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the UI runs on :5173 and proxies /api to the FastAPI server on :8000 (same-origin cookies).
// Production/demo: `npm run build` writes dist/, which FastAPI serves on :8000.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", sourcemap: false },
});
