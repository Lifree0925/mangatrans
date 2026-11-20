import fs from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// https://vite.dev/config/
const pkg = JSON.parse(fs.readFileSync("./package.json", "utf-8"));

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  plugins: [react(), tailwindcss()],
  clearScreen: false,
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    proxy: {
      // 同源代理：无论从 localhost 还是局域网 IP 访问，API 都转发到后端
      "/api": {
        target: process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
});
