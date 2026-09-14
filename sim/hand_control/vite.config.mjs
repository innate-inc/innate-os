import { defineConfig } from "vite";
export default defineConfig({
  publicDir: ".cache",
  worker: { format: "iife" },
  server: {
    proxy: {
      "/control": {
        target: "ws://127.0.0.1:8840",
        ws: true,
        changeOrigin: true,
      },
      "/robot": { target: "http://127.0.0.1:8840", changeOrigin: true },
    },
  },
});
