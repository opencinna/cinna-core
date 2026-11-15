import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import react from "@vitejs/plugin-react-swc"
import { defineConfig, loadEnv, type Plugin } from "vite"

/**
 * The SPA's Content-Security-Policy as a build-time <meta> (build only, so
 * dev HMR keeps working). `frame-ancestors`, X-Frame-Options, nosniff,
 * Referrer-Policy and HSTS are nginx headers (`nginx.conf`), because a meta
 * CSP cannot carry frame-ancestors. `API`/`WS` are the origin of
 * VITE_API_URL, omitted when the API is same-origin (empty or relative).
 * `https:` in `frame-src` and `connect-src` covers an artifacts origin
 * (`ARTIFACTS_CONTENT_ORIGIN`) unknown at build time: it is framed, and
 * `useFileText` / the pdf.js viewer fetch from it.
 */
export function buildContentSecurityPolicy(apiUrl: string | undefined): string {
  let api = ""
  let ws = ""
  if (apiUrl && /^https?:\/\//i.test(apiUrl)) {
    api = new URL(apiUrl).origin
    ws = api.replace(/^http/i, "ws")
  }
  const join = (...parts: string[]) => parts.filter(Boolean).join(" ")
  return [
    "default-src 'self'",
    "script-src 'self' https://accounts.google.com/gsi/client",
    "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style",
    // API: brand-theme assets are served by the backend, over http locally.
    join("img-src 'self' data: blob:", api, "https:"),
    "font-src 'self' data:",
    join(
      "connect-src 'self'",
      api,
      ws,
      "https://accounts.google.com/gsi/",
      "https:",
    ),
    join("frame-src 'self' blob:", api, "https:"),
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ")
}

function contentSecurityPolicyMeta(apiUrl: string | undefined): Plugin {
  return {
    name: "cinna-csp-meta",
    apply: "build",
    transformIndexHtml() {
      return [
        {
          tag: "meta",
          attrs: {
            "http-equiv": "Content-Security-Policy",
            content: buildContentSecurityPolicy(apiUrl),
          },
          injectTo: "head-prepend",
        },
      ]
    },
  }
}

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
        // Artifact content (/a/v/<cap>/…) is served by the backend with its own
        // sandbox CSP; see docs/infrastructure/nginx_setup.md.
        "^/a/": {
          target: apiUrl,
          changeOrigin: true,
        },
        // Dashboard component frames (/d/v/<cap>/…), same content host as /a/.
        "^/d/": {
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
      contentSecurityPolicyMeta(env.VITE_API_URL),
    ],
  }
})
