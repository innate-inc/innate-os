import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { execSync } from "child_process";

// https://vite.dev/config/
export default defineConfig({
  base: "/static/",
  plugins: [react()],
  define: {
    // Pull version from package.json
    __APP_VERSION__: JSON.stringify(process.env.npm_package_version),
    // Or read from a command: e.g., current HEAD commit short hash
    __COMMIT_HASH__: JSON.stringify(
      execSync("git rev-parse --short HEAD").toString().trim()
    ),
  },
});
