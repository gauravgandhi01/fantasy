import { mkdir, readFile, readdir, writeFile, rename, unlink } from "node:fs/promises";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";

export function validateSnapshot(snapshot, leagueId) {
  const fail = () => { throw new Error("Invalid or incomplete season snapshot"); };
  if (!snapshot || snapshot.metadata?.leagueId !== leagueId || !Array.isArray(snapshot.teams)
    || !Array.isArray(snapshot.completedWeeks) || !Array.isArray(snapshot.warnings)) fail();
  const m = snapshot.metadata;
  if (!Number.isInteger(m.teamCount) || m.teamCount < 1 || snapshot.teams.length !== m.teamCount
    || !Number.isInteger(m.startWeek) || !Number.isInteger(m.regularSeasonEnd)
    || m.startWeek < 1 || m.regularSeasonEnd > 18 || m.startWeek > m.regularSeasonEnd
    || !Number.isInteger(m.currentWeek) || !Number.isInteger(m.lastCompletedWeek)
    || m.lastCompletedWeek < m.startWeek - 1 || m.lastCompletedWeek > m.regularSeasonEnd) fail();
  const ids = new Set(snapshot.teams.map((t) => t.rosterId));
  if (ids.size !== m.teamCount || [...ids].some((id) => !Number.isInteger(id) || id < 1)
    || snapshot.teams.some((t) => typeof t.teamName !== "string" || typeof t.managerName !== "string")) fail();
  const validateWeek = (week) => {
    if (!week || !Number.isInteger(week.week) || week.week < m.startWeek || week.week > m.regularSeasonEnd
      || !Array.isArray(week.entries) || week.entries.length !== ids.size
      || new Set(week.entries.map((e) => e.rosterId)).size !== ids.size
      || week.entries.some((e) => !ids.has(e.rosterId) || !Number.isFinite(e.score)
        || (e.matchupId !== null && (!Number.isInteger(e.matchupId) || e.matchupId < 1)))) fail();
  };
  if (snapshot.completedWeeks.length !== m.lastCompletedWeek - m.startWeek + 1) fail();
  const weeks = new Set();
  for (const week of snapshot.completedWeeks) {
    validateWeek(week);
    if (week.status !== "final" || week.week > m.lastCompletedWeek || weeks.has(week.week)) fail();
    weeks.add(week.week);
  }
  if (snapshot.liveWeek) {
    validateWeek(snapshot.liveWeek);
    if (snapshot.liveWeek.status !== "live" || snapshot.liveWeek.week <= m.lastCompletedWeek
      || snapshot.liveWeek.week !== m.currentWeek) fail();
  }
  if (snapshot.currentWeekData) {
    validateWeek(snapshot.currentWeekData);
    if (snapshot.currentWeekData.week !== m.currentWeek
      || !["final", "live", "awaiting_scoring"].includes(snapshot.currentWeekData.status)) fail();
    if ((snapshot.currentWeekData.status === "final") !== (m.currentWeek <= m.lastCompletedWeek)) fail();
    if (snapshot.currentWeekData.status === "live" && !snapshot.liveWeek) fail();
  }
}

function contentHash(snapshot) {
  const { fetchedAt, ...metadata } = snapshot.metadata;
  return createHash("sha256").update(JSON.stringify({ ...snapshot, metadata })).digest("hex");
}

export class SnapshotStore {
  constructor(directory, leagueId, log = console) {
    this.directory = join(directory, leagueId);
    this.leagueId = leagueId;
    this.log = log;
    this.lastHash = null;
  }
  async atomicWrite(path, value) {
    const temporary = `${path}.${randomUUID()}.tmp`;
    try {
      await writeFile(temporary, JSON.stringify(value), { flag: "wx", mode: 0o600 });
      await rename(temporary, path);
    } finally { await unlink(temporary).catch(() => {}); }
  }
  async health() {
    await mkdir(join(this.directory, "revisions"), { recursive: true });
    const path = join(this.directory, `.health-${randomUUID()}`);
    try { await writeFile(path, "ok", { flag: "wx" }); }
    finally { await unlink(path).catch(() => {}); }
    return true;
  }
  async load() {
    await this.health();
    const archives = (await readdir(join(this.directory, "revisions"))).filter((n) => n.endsWith(".json")).sort().reverse();
    for (const path of [join(this.directory, "latest.json"), ...archives.map((n) => join(this.directory, "revisions", n))]) {
      try {
        const value = JSON.parse(await readFile(path, "utf8"));
        if (value.schemaVersion !== 1 || !Number.isFinite(Date.parse(value.lastSuccessfulFetchAt))) throw new Error("Invalid snapshot envelope");
        validateSnapshot(value.snapshot, this.leagueId);
        if (value.hash !== contentHash(value.snapshot)) throw new Error("Snapshot checksum mismatch");
        this.lastHash = value.hash;
        return value;
      } catch (error) {
        if (error.code !== "ENOENT") this.log.warn("snapshot_recovery", { path, error: error.message });
      }
    }
    return null;
  }
  async save(snapshot, timestamp) {
    validateSnapshot(snapshot, this.leagueId);
    const hash = contentHash(snapshot);
    const value = { schemaVersion: 1, lastSuccessfulFetchAt: timestamp, hash, snapshot };
    if (hash !== this.lastHash) {
      await this.atomicWrite(join(this.directory, "revisions", `${timestamp.replaceAll(":", "-")}-${hash}.json`), value);
    }
    await this.atomicWrite(join(this.directory, "latest.json"), value);
    this.lastHash = hash;
    return value;
  }
}
