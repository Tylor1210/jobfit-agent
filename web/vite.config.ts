// Dev: `npm run dev` on :5173 proxies /api to FastAPI on :8000 (`jobfit serve`).
// Prod: `npm run build` writes web/dist, which FastAPI serves at /.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", emptyOutDir: true },
});
