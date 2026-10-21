import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": "http://127.0.0.1:8770",
      "/admin": "http://127.0.0.1:8770",
      "/health": "http://127.0.0.1:8770",
      "/metrics": "http://127.0.0.1:8770",
      "/version": "http://127.0.0.1:8770",
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
