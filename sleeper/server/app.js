import express from "express";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const escapeHtml = (value) => value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");

export async function createApp({ service, publicBaseUrl = "http://localhost:4174" }) {
  const origin = new URL(publicBaseUrl).origin;
  if (!["http:", "https:"].includes(new URL(origin).protocol)) throw new Error("PUBLIC_BASE_URL must be HTTP(S)");
  const html = (await readFile(join(root, "index.html"), "utf8")).replaceAll("__PUBLIC_BASE_URL__", escapeHtml(origin));
  const app = express();
  app.disable("x-powered-by");
  app.use((req, res, next) => {
    res.set("X-Content-Type-Options", "nosniff");
    res.set("Referrer-Policy", "strict-origin-when-cross-origin");
    next();
  });
  app.use(["/api", "/healthz"], (req, res, next) => { res.set("Cache-Control", "no-store"); next(); });
  const sendDashboard = async (req, res) => {
    if (req.method === "POST" || !service.saved) await service.refresh();
    const value = service.dashboard();
    if (!value) return res.status(503).json({ error: "League data is temporarily unavailable. Please try again shortly." });
    res.json(value);
  };
  app.get("/api/dashboard", sendDashboard);
  app.post("/api/refresh", sendDashboard);
  app.get("/healthz", async (req, res) => {
    try {
      await service.store.health();
      res.json({ status: "ok", storage: "ok", dataAvailable: Boolean(service.saved), lastSuccessfulFetchAt: service.saved?.lastSuccessfulFetchAt || null, stale: service.dashboard()?.stale ?? true });
    } catch { res.status(503).json({ status: "unhealthy", storage: "unavailable", dataAvailable: Boolean(service.saved) }); }
  });
  app.get("/", (req, res) => { res.set("Cache-Control", "no-cache").type("html").send(html); });
  for (const asset of ["styles.css", "src/app.js", "src/ui.js", "public/og.png"]) {
    app.get(`/${asset}`, (req, res) => res.sendFile(join(root, asset), { maxAge: 0 }));
  }
  app.use((req, res) => res.status(404).json({ error: "Not found" }));
  app.use((error, req, res, next) => {
    console.error("request_failure", error.message);
    if (res.headersSent) return next(error);
    res.status(500).json({ error: "An unexpected server error occurred." });
  });
  return app;
}
