import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SnapshotStore, validateSnapshot } from "../server/store.js";
import { DashboardService, currentMatchups } from "../server/service.js";
import { createApp } from "../server/app.js";
import { fetchSleeperSeason } from "../src/sleeper.js";

const id = "1395542220504854528";
const log = { info() {}, warn() {}, error() {} };
function fixture() {
  return {
    metadata: { leagueId: id, name: "True League", season: "2026", teamCount: 2, startWeek: 1, regularSeasonEnd: 14, currentWeek: 2, lastCompletedWeek: 1, fetchedAt: "2026-09-09T12:00:00Z" },
    teams: [1, 2].map((rosterId) => ({ rosterId, teamName: `Team ${rosterId}`, managerName: `Manager ${rosterId}`, avatarUrl: null })),
    completedWeeks: [{ week: 1, status: "final", entries: [{ rosterId: 1, matchupId: 1, score: 100 }, { rosterId: 2, matchupId: 1, score: 90 }] }],
    liveWeek: null,
    currentWeekData: { week: 2, status: "awaiting_scoring", entries: [{ rosterId: 1, matchupId: 1, score: 0 }, { rosterId: 2, matchupId: 1, score: 0 }] },
    warnings: [],
  };
}
async function setup(t, fetchSeason = async () => fixture()) {
  const dir = await mkdtemp(join(tmpdir(), "sleeper-test-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const store = new SnapshotStore(dir, id, log);
  let clock = Date.parse("2026-09-09T12:00:00Z");
  const service = new DashboardService({ store, leagueId: id, fetchSeason, now: () => clock, log });
  await service.initialize();
  return { dir, store, service, advance: (ms) => { clock += ms; } };
}

test("scheduled, provisional, final, tied and unpaired matchup states", () => {
  const snapshot = fixture();
  assert.equal(currentMatchups(snapshot).status, "awaiting_scoring");
  assert.equal(currentMatchups(snapshot).matchups[0].leaderRosterId, null);
  snapshot.currentWeekData.status = "live";
  snapshot.currentWeekData.entries[0].score = 12.5;
  assert.equal(currentMatchups(snapshot).status, "provisional");
  assert.equal(currentMatchups(snapshot).matchups[0].margin, 12.5);
  snapshot.currentWeekData.status = "final";
  assert.equal(currentMatchups(snapshot).status, "final");
  snapshot.currentWeekData.entries[1].matchupId = null;
  assert.equal(currentMatchups(snapshot).matchups.length, 0);
  assert.equal(currentMatchups(snapshot).unpairedTeams.length, 2);
  snapshot.metadata.currentWeek = 15;
  assert.equal(currentMatchups(snapshot).status, "season_complete");
});

test("store persists, deduplicates timestamps, archives corrections, recovers corrupted latest", async (t) => {
  const { store, dir } = await setup(t);
  const snapshot = fixture();
  await store.save(snapshot, "2026-09-09T12:00:00Z");
  snapshot.metadata.fetchedAt = "2026-09-09T12:01:00Z";
  await store.save(snapshot, "2026-09-09T12:01:00Z");
  const revisions = join(dir, id, "revisions");
  assert.equal((await readdir(revisions)).length, 1);
  snapshot.completedWeeks[0].entries[0].score = 101;
  await store.save(snapshot, "2026-09-09T12:02:00Z");
  assert.equal((await readdir(revisions)).length, 2);
  assert.equal((await new SnapshotStore(dir, id, log).load()).lastSuccessfulFetchAt, "2026-09-09T12:02:00Z");
  await writeFile(join(dir, id, "latest.json"), "broken");
  assert.equal((await new SnapshotStore(dir, id, log).load()).snapshot.completedWeeks[0].entries[0].score, 101);
  assert.ok((await readdir(join(dir, id))).every((name) => !name.endsWith(".tmp")));
});

test("validation rejects incomplete weeks, unexpected identities, duplicate teams and invalid scores", () => {
  for (const mutate of [
    (s) => s.completedWeeks.pop(),
    (s) => { s.completedWeeks[0].entries[0].rosterId = 99; },
    (s) => { s.teams[1].rosterId = 1; },
    (s) => { s.currentWeekData.entries[0].score = null; },
    (s) => { s.completedWeeks[0].entries[1].rosterId = 1; },
  ]) {
    const s = fixture(); mutate(s);
    assert.throws(() => validateSnapshot(s, id));
  }
});

test("refreshes deduplicate and cooldown applies after success and failure", async (t) => {
  let calls = 0;
  let fail = false;
  const { service, advance } = await setup(t, async () => { calls++; if (fail) throw new Error("offline"); return fixture(); });
  await Promise.all([service.refresh(), service.refresh(), service.refresh()]);
  assert.equal(calls, 1);
  await service.refresh(); assert.equal(calls, 1);
  advance(60000); fail = true;
  await service.refresh(); await service.refresh();
  assert.equal(calls, 2);
  assert.equal(service.dashboard().stale, true);
  assert.equal(service.dashboard().stats.teams[0].total, 100);
  advance(60000); fail = false; await service.refresh();
  assert.equal(service.dashboard().stale, false);
  advance(20 * 60000 + 1); assert.equal(service.dashboard().stale, true);
});

test("invalid refresh and disk write failure preserve saved data and timestamp", async (t) => {
  const { service, store, advance } = await setup(t);
  await service.refresh();
  const saved = service.saved;
  service.fetchSeason = async () => { const s = fixture(); s.completedWeeks = []; return s; };
  advance(60000); await service.refresh(); assert.equal(service.saved, saved);
  service.fetchSeason = async () => fixture();
  store.save = async () => { throw new Error("disk full"); };
  advance(60000); await service.refresh(); assert.equal(service.saved, saved);
});

test("HTTP serves isolated assets, freshness, initialization failures and durable fallback", async (t) => {
  const { service, advance, dir } = await setup(t, async () => { throw new Error("offline"); });
  const app = await createApp({ service, publicBaseUrl: "https://league.example" });
  const server = app.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(`${base}/api/dashboard`)).status, 503);
  assert.equal((await fetch(`${base}/healthz`)).status, 200);
  const html = await (await fetch(base)).text();
  assert.match(html, /https:\/\/league.example\/public\/og.png/);
  for (const path of ["/server/index.js", "/src/sleeper.js", "/src/config.js", "/tests/backend.test.js", "/package.json", "/.data/latest.json", "/render.yaml", "/build_true_league_dashboard.py"]) {
    assert.equal((await fetch(base + path)).status, 404, path);
  }
  assert.equal((await fetch(`${base}/src/ui.js`)).status, 200);
  service.fetchSeason = async () => fixture(); advance(60000);
  const fresh = await fetch(`${base}/api/refresh`, { method: "POST" });
  assert.equal(fresh.headers.get("cache-control"), "no-store");
  assert.equal((await fresh.json()).currentWeekMatchups.matchups.length, 1);
  service.fetchSeason = async () => { throw new Error("Sleeper outage"); }; advance(60000);
  const stale = await fetch(`${base}/api/refresh`, { method: "POST" });
  assert.equal(stale.status, 200); assert.equal((await stale.json()).stale, true);
  const restarted = new DashboardService({ store: new SnapshotStore(dir, id, log), leagueId: id, log });
  await restarted.initialize(); assert.equal(restarted.dashboard().stats.teams[0].total, 100);
  service.store.health = async () => { throw new Error("disk unavailable"); };
  assert.equal((await fetch(`${base}/healthz`)).status, 503);
});

function adapterFetch({ season = "2026", type = "regular", leg = 1, last = null, partial = false, override = null, score = 0 } = {}) {
  const routes = {
    [`/league/${id}`]: { league_id: id, name: "Test", season: "2026", total_rosters: 2, status: "in_season", settings: { leg, start_week: 1, playoff_week_start: 15, last_scored_leg: last } },
    [`/league/${id}/users`]: [],
    [`/league/${id}/rosters`]: [{ roster_id: 1 }, { roster_id: 2 }],
    "/state/nfl": { season, season_type: type, leg },
  };
  return async (url) => {
    const path = url.replace("https://api.sleeper.app/v1", "");
    const body = routes[path] || (path.includes("/matchups/") ? [{ roster_id: 1, matchup_id: 1, points: score, custom_points: override }, ...partial ? [] : [{ roster_id: 2, matchup_id: 1, points: 0 }]] : null);
    return { ok: Boolean(body), status: body ? 200 : 404, json: async () => body };
  };
}
test("adapter preserves zero schedules and custom zero, rejects partial refreshes", async () => {
  const fetchSeason = (options) => fetchSleeperSeason({ leagueId: id, strict: true, fetchImpl: adapterFetch(options) });
  const waiting = await fetchSeason({});
  assert.equal(waiting.currentWeekData.status, "awaiting_scoring");
  assert.equal(waiting.completedWeeks.length, 0);
  assert.equal(waiting.liveWeek, null);
  const live = await fetchSeason({ score: 40, override: 0 });
  assert.equal(live.currentWeekData.entries[0].score, 0);
  assert.equal(live.currentWeekData.status, "live");
  await assert.rejects(fetchSeason({ partial: true }), /incomplete/);
  const final = await fetchSeason({ last: 1 });
  assert.equal(final.currentWeekData.status, "final");
  assert.equal(final.completedWeeks.length, 1);
});
test("adapter distinguishes preseason, explicit zero completion, and postseason", async () => {
  for (const options of [{ type: "post", leg: 18 }, { season: "2027", type: "pre", leg: 1 }, { leg: 15 }]) {
    const s = await fetchSleeperSeason({ leagueId: id, strict: true, fetchImpl: adapterFetch(options) });
    assert.ok(s.metadata.currentWeek > 14);
    assert.equal(s.completedWeeks.length, 14);
    assert.equal(s.liveWeek, null);
    assert.equal(currentMatchups(s).status, "season_complete");
  }
  const pre = await fetchSleeperSeason({ leagueId: id, strict: true, fetchImpl: adapterFetch({ season: "2025", type: "post", leg: 18 }) });
  assert.equal(pre.completedWeeks.length, 0);
  assert.equal(pre.metadata.currentWeek, 1);
  const explicit = await fetchSleeperSeason({ leagueId: id, strict: true, fetchImpl: adapterFetch({ leg: 2, last: 0 }) });
  assert.equal(explicit.completedWeeks.length, 0);
});

test("browser entrypoint only fetches local APIs and does not poll", async () => {
  const source = await readFile(new URL("../src/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /api\.sleeper|localStorage|setInterval/);
  assert.match(source, /\/api\/refresh/);
});
