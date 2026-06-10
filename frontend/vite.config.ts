import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: "../kol_archive/web_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/posts": "http://127.0.0.1:8765",
      "/rewrite-exercises": "http://127.0.0.1:8765",
    },
  },
});
