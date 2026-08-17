import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { nodePolyfills } from "vite-plugin-node-polyfills";

export default defineConfig({
  plugins: [
    // GramJS needs Node globals polyfilled in the browser (same as TG Drive).
    nodePolyfills({
      globals: {
        Buffer: true,
        global: true,
        process: true,
      },
      protocolImports: true,
    }),
    tailwindcss(),
    react(),
  ],
  define: {
    global: "globalThis",
  },
  build: {
    outDir: "dist",
    target: "es2022",
  },
});
