import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs on 8000; proxying keeps the frontend origin-relative so the same build
// works behind Cloud Run without a rebuild.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true,
                rewrite: (p) => p.replace(/^\/api/, "") },
    },
  },
});
