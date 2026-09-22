// Lenovmail — authored by satuapps
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Build output goes to `web/dist`, which Docker copies into `/app/static` and serves via
// FastAPI. `assets/` is intentionally left at its default so it matches the `/assets` mount.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // During `npm run dev`, all API/SSE calls are proxied to uvicorn on 8080.
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true, ws: false },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
