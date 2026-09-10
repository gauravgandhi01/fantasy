import assert from "assert";
import { readSnapshotCache, writeSnapshotCache } from "../src/cache.js";
import { fetchJson, fetchSleeperSeason, SleeperApiError } from "../src/sleeper.js";
import {
  calculateStats,
  competitionRanks,
  median,
  populationStandardDeviation,
  sampleStandardDeviation,
} from "../src/stats.js";

let passed = 0;

async function test(name, operation) {
  try {
    await operation();
    passed += 1;
    console.log(`✓ ${name}`);
  } catch (error) {
    console.error(`✗ ${name}`);
    throw error;
  }
}

function close(actual, expected, tolerance = 1e-9) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not within ${tolerance} of ${expected}`);
}

function team(rosterId, teamName) {
  return {
    rosterId,
    ownerIds: [`user-${rosterId}`],
    managerNames: [teamName],
    managerName: teamName,
    teamName,
    avatarUrl: null,
  };
}

function snapshotFromScores(names, scoreRows) {
  return {
    metadata: {
      leagueId: "2025-fixture",
      name: "True League",
      season: "2025",
      status: "complete",
      teamCount: names.length,
      startWeek: 1,
      regularSeasonEnd: scoreRows[0].length,
      playoffWeekStart: scoreRows[0].length + 1,
      currentWeek: scoreRows[0].length,
      lastCompletedWeek: scoreRows[0].length,
      seasonStartDate: null,
      beforeSeasonStart: false,
      fetchedAt: "2026-01-01T00:00:00.000Z",
      source: "fixture",
    },
    teams: names.map((name, index) => team(index + 1, name)),
    completedWeeks: scoreRows[0].map((_, weekIndex) => ({
      week: weekIndex + 1,
      status: "final",
      entries: scoreRows.map((scores, rosterIndex) => ({
        rosterId: rosterIndex + 1,
        matchupId: Math.floor(rosterIndex / 2) + 1,
        score: scores[weekIndex],
      })),
    })),
    liveWeek: null,
    warnings: [],
  };
}

const names2025 = ["G", "ZJ", "Will", "Jake", "HG", "Ethan", "Dylan", "Alex", "Cameron", "Dross"];
const scores2025 = [
  [115.32, 155.48, 118.86, 162.4, 109.52, 141.78, 121.74, 129.76, 114.2, 116.92, 134.34, 114.78, 119.54, 139.4],
  [116.92, 106.24, 101.84, 113.58, 149.02, 122.98, 131.58, 104.96, 168.5, 135.1, 142.72, 137.16, 96.56, 117.34],
  [91.32, 118.4, 118.82, 129.68, 123.34, 147.74, 125.04, 125.82, 90.82, 149.98, 111.48, 124.68, 139.26, 130.02],
  [93.38, 121.14, 162.64, 112.4, 79.8, 92.72, 172.54, 146.86, 98.54, 149.52, 85.4, 176.76, 113.3, 118.24],
  [84.42, 155.74, 107.02, 94.28, 162.22, 111.58, 129.74, 164, 114.72, 130.6, 100.86, 103.46, 111.34, 109.36],
  [127.16, 97.02, 116.52, 164.56, 122.12, 110.4, 131.66, 97.42, 158.72, 102.94, 108.18, 92.22, 115.72, 99.04],
  [124.52, 85.8, 107.52, 105.18, 143.32, 117.28, 97.12, 111.98, 114.78, 123.1, 121.02, 127.34, 111.54, 128.4],
  [88.94, 79.78, 124.02, 129.44, 110.18, 76.86, 128.56, 88.98, 112.9, 118.7, 151, 81.52, 138.24, 92.78],
  [97.38, 121.9, 122.32, 115.16, 100.32, 118.94, 116.82, 105.78, 106.96, 89, 80.44, 122.76, 82.98, 89.48],
  [113.86, 128.1, 91.32, 110.58, 87.04, 86.36, 104.3, 122.46, 117.76, 121.54, 105.42, 94.22, 72.44, 96.66],
];

await test("median handles odd and even score sets", () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 3, 2]), 2.5);
  assert.equal(median([]), null);
});

await test("sample and population deviations use their intended denominators", () => {
  close(sampleStandardDeviation([2, 4, 4, 4, 5, 5, 7, 9]), 2.138089935299395);
  close(populationStandardDeviation([2, 4, 4, 4, 5, 5, 7, 9]), 2);
  assert.equal(sampleStandardDeviation([4]), null);
});

await test("competition ranks skip positions after ties", () => {
  assert.deepEqual(competitionRanks([100, 100, 90, 80]), [1, 1, 3, 4]);
});

await test("2025 fixture reconciles the spreadsheet scoring model", () => {
  const stats = calculateStats(snapshotFromScores(names2025, scores2025), { greatWeekDelta: 20 });
  close(stats.league.overallMean, 116.95928571428571);
  close(stats.league.overallMedian, 116.12);
  close(stats.league.scoreDeviation, 22.015402118093988);
  close(stats.league.medianOfWeeklyMedians, 115.305);
  close(stats.league.meanDeltaMedian, 1.0178571428571395);
  close(stats.league.medianDeltaMedian, 0, 1e-12);
  close(stats.league.deltaDeviation, 21.49449593286996);

  const g = stats.teams.find((row) => row.teamName === "G");
  close(g.total, 1794.04);
  close(g.average, 128.1457142857143);
  assert.equal(g.medianRecord, "9-5");
  close(g.medianWinPct, 9 / 14);
  close(g.averageRank, 4.142857142857143);
  close(g.averageDeltaMedian, 12.204285714285714);
  assert.equal(g.over120, 7);
  assert.equal(g.under110, 1);
  assert.equal(g.bestScore, 162.4);
  assert.equal(g.worstScore, 109.52);
  close(g.scoreRange, 52.88);
  close(g.scoreDeviation, 16.375218478535796);
  assert.deepEqual([g.greatWeeks, g.averageWeeks, g.badWeeks], [5, 9, 0]);
  close(g.rankDeviation, 2.2483204964917824);
  assert.equal(g.bestRank, 1);
  assert.equal(g.worstRank, 8);
  assert.deepEqual(stats.weeks[0].entries.map((entry) => entry.rank), [4, 3, 8, 7, 10, 1, 2, 9, 6, 5]);
});

await test("week quality uses strict ±20 median boundaries", () => {
  const boundary = calculateStats(snapshotFromScores(["High", "Mid", "Low"], [[120], [100], [80]]));
  boundary.teams.forEach((row) => assert.deepEqual([row.greatWeeks, row.averageWeeks, row.badWeeks], [0, 1, 0]));
  const outside = calculateStats(snapshotFromScores(["High", "Mid", "Low"], [[120.01], [100], [79.99]]));
  assert.deepEqual([outside.teams.find((row) => row.teamName === "High").greatWeeks, outside.teams.find((row) => row.teamName === "Low").badWeeks], [1, 1]);
});

await test("median ties are half-wins and zero scores remain valid", () => {
  const stats = calculateStats(snapshotFromScores(["High", "Tie", "Zero"], [[10], [5], [0]]));
  const tied = stats.teams.find((row) => row.teamName === "Tie");
  const zero = stats.teams.find((row) => row.teamName === "Zero");
  assert.equal(tied.medianRecord, "0-0-1");
  assert.equal(tied.medianWinPct, 0.5);
  assert.equal(zero.total, 0);
  assert.equal(zero.games, 1);
  assert.equal(zero.scoreDeviation, null);
});

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function apiFixture({ partialSecondWeek = false, livePoints = 0 } = {}) {
  const leagueId = "1395542220504854528";
  const league = {
    league_id: leagueId,
    name: "True League",
    season: "2026",
    status: "in_season",
    total_rosters: 2,
    settings: { start_week: 1, playoff_week_start: 3, last_scored_leg: partialSecondWeek ? 2 : 1, leg: 2 },
    scoring_settings: { rec: 0.5 },
  };
  const users = [
    { user_id: "100000000000000001", display_name: "Owner One", avatar: "one", metadata: { team_name: "Alpha", avatar: "https://example.com/a.jpg" } },
    { user_id: "100000000000000002", display_name: "Owner Two", avatar: null, metadata: {} },
    { user_id: "100000000000000003", display_name: "Co Owner", avatar: null, metadata: {} },
  ];
  const rosters = [
    { roster_id: 1, owner_id: "100000000000000001", co_owners: ["100000000000000003"] },
    { roster_id: 2, owner_id: "100000000000000002", co_owners: null },
  ];
  const week1 = [
    { roster_id: 1, matchup_id: 1, points: 100, custom_points: 0, players_points: { p1: 100 } },
    { roster_id: 2, matchup_id: 1, points: 90, custom_points: null, players_points: { p2: 90 } },
  ];
  const week2 = partialSecondWeek
    ? [{ roster_id: 1, matchup_id: 1, points: 80, custom_points: null, players_points: { p1: 80 } }]
    : [
        { roster_id: 1, matchup_id: 1, points: livePoints, custom_points: null, players_points: { p1: livePoints } },
        { roster_id: 2, matchup_id: 1, points: 0, custom_points: null, players_points: { p2: 0 } },
      ];
  const state = { season: "2026", season_type: "regular", leg: 2, season_start_date: "2026-09-01" };
  const routes = new Map([
    [`https://api.sleeper.app/v1/league/${leagueId}`, league],
    [`https://api.sleeper.app/v1/league/${leagueId}/users`, users],
    [`https://api.sleeper.app/v1/league/${leagueId}/rosters`, rosters],
    [`https://api.sleeper.app/v1/league/${leagueId}/matchups/1`, week1],
    [`https://api.sleeper.app/v1/league/${leagueId}/matchups/2`, week2],
    ["https://api.sleeper.app/v1/state/nfl", state],
  ]);
  return async (url) => routes.has(url) ? response(routes.get(url)) : response({}, 404);
}

await test("Sleeper adapter preserves string IDs, owners, and zero custom overrides", async () => {
  const snapshot = await fetchSleeperSeason({
    leagueId: "1395542220504854528",
    fetchImpl: apiFixture(),
    now: new Date("2026-09-04T12:00:00Z"),
  });
  assert.equal(snapshot.metadata.leagueId, "1395542220504854528");
  assert.equal(snapshot.metadata.regularSeasonEnd, 2);
  assert.equal(snapshot.metadata.scoringLabel, "Half PPR");
  assert.equal(snapshot.completedWeeks.length, 1);
  assert.equal(snapshot.completedWeeks[0].entries[0].score, 0);
  assert.equal(snapshot.liveWeek, null);
  assert.equal(snapshot.teams[0].teamName, "Alpha");
  assert.equal(snapshot.teams[0].managerName, "Owner One / Co Owner");
  assert.equal(snapshot.teams[0].avatarUrl, "https://example.com/a.jpg");
  assert.equal(snapshot.teams[1].teamName, "Owner Two");
});

await test("Sleeper adapter exposes live scoring but does not finalize it", async () => {
  const snapshot = await fetchSleeperSeason({
    leagueId: "1395542220504854528",
    fetchImpl: apiFixture({ livePoints: 5 }),
    now: new Date("2026-09-04T12:00:00Z"),
  });
  assert.equal(snapshot.completedWeeks.length, 1);
  assert.equal(snapshot.liveWeek.week, 2);
  assert.equal(snapshot.liveWeek.entries[0].score, 5);
  const stats = calculateStats(snapshot);
  assert.equal(stats.liveWeek.leagueMedian, 2.5);
  assert.deepEqual(stats.liveWeek.entries.map((entry) => entry.rank), [1, 2]);
  assert.equal(stats.league.scoreCount, 2);
  assert.equal(stats.teams.find((entry) => entry.rosterId === 1).total, 0);
});

await test("Sleeper adapter excludes and flags partial finalized weeks", async () => {
  const snapshot = await fetchSleeperSeason({
    leagueId: "1395542220504854528",
    fetchImpl: apiFixture({ partialSecondWeek: true }),
    now: new Date("2026-09-04T12:00:00Z"),
  });
  assert.equal(snapshot.completedWeeks.length, 1);
  assert.match(snapshot.warnings.join(" "), /Week 2 is incomplete/);
});

await test("Sleeper adapter reports a missing league cleanly", async () => {
  await assert.rejects(
    fetchSleeperSeason({ leagueId: "missing", fetchImpl: async () => response({}, 404) }),
    (error) => error instanceof SleeperApiError && error.status === 404,
  );
});

await test("Sleeper requests retry one transient server failure", async () => {
  let calls = 0;
  const value = await fetchJson("https://example.com/retry", async () => {
    calls += 1;
    return calls === 1 ? response({}, 503) : response({ ok: true });
  });
  assert.equal(calls, 2);
  assert.deepEqual(value, { ok: true });
});

await test("snapshot cache distinguishes fresh, stale, and corrupt data", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  assert.equal(writeSnapshotCache(storage, "league", { teams: [] }, 1000), true);
  assert.equal(readSnapshotCache(storage, "league", 1200, 500).isFresh, true);
  assert.equal(readSnapshotCache(storage, "league", 2000, 500).isFresh, false);
  values.set("league", "not json");
  assert.equal(readSnapshotCache(storage, "league", 2000, 500), null);
});

console.log(`\n${passed} tests passed.`);
