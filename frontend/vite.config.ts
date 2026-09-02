import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import react from "@vitejs/plugin-react-swc"
import { defineConfig, loadEnv } from "vite"

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")
  const apiUrl = env.VITE_API_URL || "http://localhost:8000"

  return {
    // The public Local Agent Kit entrypoint (`/agent-start`) is a backend route
    // served at the SPA origin. In production nginx proxies it (see
    // docs/infrastructure/nginx_setup.md); the dev server needs the same rule
    // or http://localhost:5173/agent-start falls through to the SPA and 404s.
    server: {
      proxy: {
        "^/agent-start(/|$)": {
          target: apiUrl,
          changeOrigin: true,
        },
      },
    },
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    plugins: [
      tanstackRouter({
        target: "react",
        autoCodeSplitting: true,
      }),
      react(),
      tailwindcss(),
    ],
  }
})
