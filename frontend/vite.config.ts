import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets land directly in the backend image at app/static.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/app/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
