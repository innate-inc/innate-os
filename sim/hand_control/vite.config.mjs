import { defineConfig, searchForWorkspaceRoot } from "vite";
export default defineConfig({
  publicDir: ".cache",
  worker: { format: "iife" },
  build: { rollupOptions: { input: ["index.html", "match.html"] } },
  server: {
    // The studio imports the shared hand mapping from the robot's webapp.
    fs: {
      allow: [
        searchForWorkspaceRoot(process.cwd()),
        "../../webapp/js/handControl",
      ],
    },
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
