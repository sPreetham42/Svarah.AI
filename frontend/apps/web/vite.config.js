import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SVARAH_API_URL / SVARAH_WEB_PORT let a second stack (e.g. an offline mock
// backend) run beside the usual one.
const API_PROXY = {
  "/api": {
    target: process.env.SVARAH_API_URL || "http://localhost:8000",
    changeOrigin: true,
    // The live-transcription preview is a WebSocket, so the dev proxy has to
    // forward upgrades as well as plain requests.
    ws: true,
  },
};

/**
 * Vite's static server has no media type for `.apk`, and a phone decides
 * whether to offer "Install" from that header. Production hosts set it
 * themselves; this covers `vite` and `vite preview`.
 */
const APK_TYPE = "application/vnd.android.package-archive";
/** `root` is the folder the server serves files from (public/ or dist/). */
const apkMediaType = (root) => (req, res, next) => {
  const url = decodeURIComponent(req.url?.split("?")[0] ?? "");
  // A missing APK falls through to the site's HTML, which must keep its type
  // so the download page can tell that no build is published.
  if (url.endsWith(".apk") && fs.existsSync(path.join(root, url))) {
    const writeHead = res.writeHead;
    // The static middleware passes its own (empty) Content-Type to writeHead,
    // which would override a header set here, so the fix is applied there.
    res.writeHead = function patched(status, ...rest) {
      const headers = rest.find((item) => item && typeof item === "object" && !Array.isArray(item));
      if (headers) headers["Content-Type"] = APK_TYPE;
      else res.setHeader("Content-Type", APK_TYPE);
      return writeHead.call(this, status, ...rest);
    };
  }
  next();
};
const apkDownloads = {
  name: "svarah-apk-media-type",
  configureServer: (server) => void server.middlewares.use(apkMediaType(server.config.publicDir)),
  configurePreviewServer: (server) =>
    void server.middlewares.use(apkMediaType(path.resolve(server.config.root, server.config.build.outDir))),
};

export default defineConfig({
  plugins: [react(), apkDownloads],
  server: {
    // Listen on IPv4 so the site answers on http://127.0.0.1:5173 as well as
    // http://localhost:5173 — Firebase phone sign-in rejects real numbers
    // from "localhost".
    host: "127.0.0.1",
    port: Number(process.env.SVARAH_WEB_PORT) || 5173,
    proxy: API_PROXY,
  },
  preview: {
    port: 4173,
    proxy: API_PROXY,
  },
});
