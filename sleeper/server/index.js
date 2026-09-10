import { fileURLToPath } from "node:url";
import { SnapshotStore } from "./store.js";
import { DashboardService } from "./service.js";
import { createApp } from "./app.js";

const leagueId = process.env.LEAGUE_ID || "1395542220504854528";
if (!/^\d+$/.test(leagueId)) throw new Error("LEAGUE_ID must be a numeric string");
const port = Number(process.env.PORT || 4174);
const directory = process.env.DATA_DIR || fileURLToPath(new URL("../.data", import.meta.url));
const service = new DashboardService({ store: new SnapshotStore(directory, leagueId), leagueId });
await service.initialize();
const app = await createApp({ service, publicBaseUrl: process.env.PUBLIC_BASE_URL || process.env.RENDER_EXTERNAL_URL || `http://localhost:${port}` });
const server = app.listen(port, "0.0.0.0", () => console.info("server_ready", { port }));
void service.refresh();
const timer = setInterval(() => { void service.refresh(); }, 15 * 60000);
timer.unref();
for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, () => {
  clearInterval(timer);
  server.close(async () => { await service.pending; process.exit(0); });
});
