import { defineConfig } from "vite";

export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: new URL("./index.html", import.meta.url).pathname,
        pet: new URL("./pet.html", import.meta.url).pathname,
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: Number(process.env.WAILS_VITE_PORT) || 9245,
    strictPort: true,
  },
  resolve: {
    alias: {
      "@wailsio/runtime": new URL("./public/wails-runtime.js", import.meta.url).pathname,
    },
  },
});
