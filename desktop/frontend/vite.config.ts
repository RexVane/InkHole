import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";

export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        pet: fileURLToPath(new URL("./pet.html", import.meta.url)),
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: Number(process.env.TAURI_VITE_PORT) || 9245,
    strictPort: true,
  },
  resolve: {
    alias: {
      "@wailsio/runtime": fileURLToPath(new URL("./src/tauri-runtime.ts", import.meta.url)),
    },
  },
});
