const API_BASE = "https://api.sleeper.app/v1";
const RETRYABLE_STATUS = new Set([429, 500, 502, 503, 504]);

export class SleeperApiError extends Error {
  constructor(message, status = null) {
    super(message);
    this.name = "SleeperApiError";
    this.status = status;
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Fetch JSON, retrying one transient Sleeper failure. */
export async function fetchJson(url, fetchImpl = globalThis.fetch) {
  if (typeof fetchImpl !== "function") {
    throw new SleeperApiError("This browser does not support network requests.");
  }

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), 12000) : null;
    try {
      const response = await fetchImpl(url, {
        headers: { Accept: "application/json" },
        signal: controller?.signal,
      });
      if (!response.ok) {
        const error = new SleeperApiError(
          response.status === 404
            ? "Sleeper could not find this league."
            : `Sleeper returned HTTP ${response.status}.`,
          response.status,
        );
        if (attempt === 0 && RETRYABLE_STATUS.has(response.status)) {
          await wait(350);
          continue;
        }
        throw error;
      }
      return await response.json();
    } catch (error) {
      const isAbort = error?.name === "AbortError";
      const isRetryableNetworkError = !(error instanceof SleeperApiError);
      if (attempt === 0 && (isAbort || isRetryableNetworkError)) {
        await wait(350);
        continue;
      }
      if (error instanceof SleeperApiError) throw error;
      throw new SleeperApiError(
        isAbort ? "Sleeper took too long to respond." : "Could not reach Sleeper.",
      );
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  }
  throw new SleeperApiError("Could not reach Sleeper.");
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}

function avatarUrl(user) {
  const metadataAvatar = String(user?.metadata?.avatar || "").trim();
  if (/^https:\/\//i.test(metadataAvatar)) return metadataAvatar;
  const avatarId = String(user?.avatar || "").trim();
  return avatarId ? `https://sleepercdn.com/avatars/thumbs/${encodeURIComponent(avatarId)}` : null;
}

function scoringLabel(scoringSettings) {
  const receptions = Number(scoringSettings?.rec);
  if (!Number.isFinite(receptions)) return "Custom scoring";
  if (receptions === 0) return "Standard";
  if (receptions === 0.5) return "Half PPR";
  if (receptions === 1) return "PPR";
  return `${receptions} PPR`;
}

function normalizeTeams(users, rosters, warnings) {
  const usersById = new Map(
    users
      .filter((user) => user?.user_id != null)
      .map((user) => [String(user.user_id), user]),
  );

  return [...rosters]
    .sort((a, b) => Number(a.roster_id) - Number(b.roster_id))
    .map((roster) => {
      const rosterId = Number(roster.roster_id);
      const ownerIds = [roster.owner_id, ...(roster.co_owners || [])]
        .filter((id) => id != null && String(id).trim())
        .map(String);
      const ownerUsers = ownerIds.map((id) => usersById.get(id)).filter(Boolean);
      const managerNames = [...new Set(ownerUsers
        .map((user) => String(user.display_name || "").trim())
        .filter(Boolean))];
      const primary = usersById.get(String(roster.owner_id));
      const apiTeamName = String(primary?.metadata?.team_name || "").trim();
      const managerName = managerNames.join(" / ") || `Roster ${rosterId}`;
      if (!primary) warnings.push(`Roster ${rosterId} has no matching Sleeper user.`);

      return {
        rosterId,
        ownerIds,
        managerNames,
        managerName,
        teamName: apiTeamName || String(primary?.display_name || "").trim() || `Roster ${rosterId}`,
        avatarUrl: avatarUrl(primary),
      };
    });
}

function scoreForEntry(entry) {
  const raw = entry.custom_points !== null && entry.custom_points !== undefined
    ? entry.custom_points
    : entry.points;
  if (raw == null || raw === "" || typeof raw === "boolean") return null;
  const score = Number(raw);
  return Number.isFinite(score) ? score : null;
}

function hasLiveScoring(rawEntries) {
  return rawEntries.some((entry) => {
    if (entry.custom_points !== null && entry.custom_points !== undefined) return true;
    if (Number(entry.points) !== 0) return true;
    return Object.values(entry.players_points || {}).some((value) => Number(value) !== 0);
  });
}

function normalizeWeek(week, rawEntries, status) {
  return {
    week,
    status,
    entries: rawEntries.map((entry) => ({
      rosterId: Number(entry.roster_id),
      matchupId: entry.matchup_id == null ? null : Number(entry.matchup_id),
      score: scoreForEntry(entry),
    })),
  };
}

function completeWeekIsValid(week, teamCount) {
  const ids = new Set(week.entries.map((entry) => entry.rosterId));
  return week.entries.length === teamCount
    && ids.size === teamCount
    && week.entries.every((entry) => Number.isFinite(entry.score));
}

function resolveLastCompletedWeek(league, state, startWeek, regularSeasonEnd) {
  const raw = league.settings?.last_scored_leg;
  const explicit = Number(raw);
  if (raw != null && Number.isInteger(explicit) && explicit >= 0) {
    return Math.max(startWeek - 1, Math.min(explicit, regularSeasonEnd));
  }
  if (league.status === "complete") return regularSeasonEnd;

  const leagueSeason = Number(league.season);
  const stateSeason = Number(state.season);
  if (Number.isFinite(stateSeason) && Number.isFinite(leagueSeason) && stateSeason > leagueSeason) {
    return regularSeasonEnd;
  }
  if (stateSeason !== leagueSeason || state.season_type === "pre") return startWeek - 1;
  if (state.season_type === "post") return regularSeasonEnd;

  const currentLeg = positiveInteger(league.settings?.leg, positiveInteger(state.leg, startWeek));
  return Math.min(regularSeasonEnd, Math.max(startWeek - 1, currentLeg - 1));
}

/**
 * Fetch and normalize a single Sleeper regular season.
 * Sleeper IDs remain strings because they exceed JavaScript's safe integer range.
 *
 * @param {{leagueId:string, fetchImpl?:Function, now?:Date, strict?:boolean}} input
 * @returns {Promise<import('./stats.js').SeasonSnapshot>}
 */
export async function fetchSleeperSeason({
  leagueId,
  fetchImpl = globalThis.fetch,
  now = new Date(),
  strict = false,
}) {
  const normalizedLeagueId = String(leagueId);
  const leagueUrl = `${API_BASE}/league/${encodeURIComponent(normalizedLeagueId)}`;
  const [league, users, rosters, state] = await Promise.all([
    fetchJson(leagueUrl, fetchImpl),
    fetchJson(`${leagueUrl}/users`, fetchImpl),
    fetchJson(`${leagueUrl}/rosters`, fetchImpl),
    fetchJson(`${API_BASE}/state/nfl`, fetchImpl),
  ]);

  if (!league || String(league.league_id || "") !== normalizedLeagueId) {
    throw new SleeperApiError("Sleeper returned an invalid league response.");
  }
  if (!Array.isArray(users) || !Array.isArray(rosters) || !state || !Number.isInteger(Number(state.season))) {
    throw new SleeperApiError("Sleeper returned an invalid roster response.");
  }

  const warnings = [];
  const startWeek = positiveInteger(league.settings?.start_week, 1);
  const playoffWeekStart = positiveInteger(league.settings?.playoff_week_start, null);
  const regularSeasonEnd = playoffWeekStart ? playoffWeekStart - 1 : 18;
  if (!playoffWeekStart) {
    warnings.push("Playoff start was unavailable; regular-season Week 18 was used as a fallback.");
  }
  const teamCount = positiveInteger(league.total_rosters, rosters.length);
  const sameSeason = String(state.season) === String(league.season);
  const currentWeek = Number(state.season) > Number(league.season) || league.status === "complete"
    ? regularSeasonEnd + 1
    : !sameSeason || state.season_type === "pre" ? startWeek
      : state.season_type === "post" ? Math.max(regularSeasonEnd + 1, positiveInteger(state.leg, 1))
        : positiveInteger(league.settings?.leg, positiveInteger(state.leg, startWeek));
  const lastCompletedWeek = resolveLastCompletedWeek(
    league,
    state,
    startWeek,
    regularSeasonEnd,
  );
  const weekNumbers = [];
  const rosterIds = new Set(rosters.map((roster) => Number(roster.roster_id)));
  if (rosters.length !== teamCount || rosterIds.size !== teamCount
    || [...rosterIds].some((id) => !Number.isInteger(id) || id < 1)) {
    throw new SleeperApiError("Sleeper returned incomplete or duplicate rosters.");
  }
  for (let week = startWeek; week <= lastCompletedWeek; week += 1) weekNumbers.push(week);
  if (currentWeek >= startWeek && currentWeek <= regularSeasonEnd && currentWeek > lastCompletedWeek) {
    weekNumbers.push(currentWeek);
  }

  const matchupPayloads = await Promise.all(weekNumbers.map(async (week) => ({
    week,
    data: await fetchJson(`${leagueUrl}/matchups/${week}`, fetchImpl),
  })));

  const completedWeeks = [];
  let liveWeek = null;
  let currentWeekData = null;
  matchupPayloads.forEach(({ week, data }) => {
    if (!Array.isArray(data)) {
      if (strict) throw new SleeperApiError(`Week ${week} returned an invalid matchup response.`);
      warnings.push(`Week ${week} returned an invalid matchup response and was excluded.`);
      return;
    }
    if (week <= lastCompletedWeek) {
      const normalized = normalizeWeek(week, data, "final");
      const valid = completeWeekIsValid(normalized, teamCount) && normalized.entries.every((entry) => rosterIds.has(entry.rosterId));
      if (valid) {
        completedWeeks.push(normalized);
        if (week === currentWeek) currentWeekData = normalized;
      } else if (strict) throw new SleeperApiError(`Week ${week} is incomplete; saved data was retained.`);
      else warnings.push(`Week ${week} is incomplete and was excluded from season statistics.`);
      return;
    }
    if (week === currentWeek && data.length) {
      const status = hasLiveScoring(data) ? "live" : "awaiting_scoring";
      const normalized = normalizeWeek(week, data, status);
      if (!completeWeekIsValid(normalized, teamCount) || !normalized.entries.every((entry) => rosterIds.has(entry.rosterId))) {
        if (strict) throw new SleeperApiError(`Week ${week} is incomplete; saved data was retained.`);
        warnings.push(`Week ${week} is incomplete and was excluded.`);
        return;
      }
      currentWeekData = normalized;
      if (status === "live") liveWeek = normalized;
    }
  });

  const teams = normalizeTeams(users, rosters, warnings);
  const seasonStart = state.season_start_date ? new Date(`${state.season_start_date}T00:00:00`) : null;
  const beforeSeasonStart = seasonStart && !Number.isNaN(seasonStart.valueOf())
    ? now.valueOf() < seasonStart.valueOf()
    : completedWeeks.length === 0 && !liveWeek;

  return {
    metadata: {
      leagueId: normalizedLeagueId,
      name: String(league.name || "Fantasy League"),
      season: String(league.season || state.season || ""),
      status: String(league.status || "unknown"),
      teamCount,
      startWeek,
      regularSeasonEnd,
      playoffWeekStart: regularSeasonEnd + 1,
      currentWeek,
      lastCompletedWeek,
      scoringLabel: scoringLabel(league.scoring_settings),
      seasonStartDate: state.season_start_date || null,
      beforeSeasonStart: Boolean(beforeSeasonStart),
      fetchedAt: now.toISOString(),
      source: "Sleeper API",
    },
    teams,
    completedWeeks,
    liveWeek,
    currentWeekData,
    warnings,
  };
}
