import path from "node:path";
import {fileURLToPath} from "node:url";

const appRoot = path.dirname(fileURLToPath(import.meta.url));
const workspaceRoot = path.resolve(appRoot, "../../..");
const frontendRoot = path.join(workspaceRoot, "desktop", "frontend");

export default {
  root: frontendRoot,
  base: "./",
  build: {
    emptyOutDir: true,
    outDir: path.join(appRoot, "dist"),
    rollupOptions: {
      input: {
        main: path.join(frontendRoot, "index.html"),
        pet: path.join(frontendRoot, "pet.html"),
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 9245,
    strictPort: true,
    fs: {
      allow: [frontendRoot, appRoot],
    },
  },
  resolve: {
    alias: {
      "@wailsio/runtime": path.join(workspaceRoot, "desktop", "frontend", "src", "tauri-runtime.ts"),
    },
  },
};
