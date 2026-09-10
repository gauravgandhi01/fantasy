const COLORS = [
  "#27785b", "#d46a55", "#6676d8", "#c48b22", "#8b5fc7",
  "#2e91a3", "#b65686", "#58843c", "#a86637", "#476f95",
];

const uiState = {
  statSort: { key: "total", direction: "desc" },
  selectedRosters: new Set(),
  matrixMode: "score",
};

let refreshHandler = () => {};

function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null && text !== undefined) node.textContent = String(text);
  return node;
}

function svgEl(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function byId(id) {
  return document.getElementById(id);
}

function point(value, digits = 2) {
  return Number.isFinite(value) ? Number(value).toFixed(digits) : "—";
}

function rank(value) {
  return Number.isFinite(value) ? Number(value).toFixed(1) : "—";
}

function integer(value) {
  return Number.isFinite(value) ? String(Math.round(value)) : "—";
}

function percentage(value) {
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
}

function signed(value, digits = 2) {
  if (!Number.isFinite(value)) return "—";
  if (value === 0) return Number(value).toFixed(digits);
  return `${value > 0 ? "+" : ""}${Number(value).toFixed(digits)}`;
}

function dateTime(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Unknown time";
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function teamInitials(team) {
  const parts = team.teamName.split(/\s+/).filter(Boolean);
  return (parts.length > 1 ? `${parts[0][0]}${parts[1][0]}` : parts[0]?.slice(0, 2) || "TL").toUpperCase();
}

function makeAvatar(team) {
  const avatar = el("span", "team-avatar", teamInitials(team));
  if (team.avatarUrl) {
    const image = el("img");
    image.src = team.avatarUrl;
    image.alt = "";
    image.loading = "lazy";
    image.addEventListener("error", () => { image.hidden = true; }, { once: true });
    avatar.append(image);
  }
  return avatar;
}

function makeTeamLabel(team) {
  const label = el("span", "team-label");
  const copy = el("span", "team-copy");
  copy.append(el("strong", "", team.teamName), el("span", "", team.managerName));
  label.append(makeAvatar(team), copy);
  return label;
}

function emptyState(title, body, mark = "TL") {
  const node = el("div", "empty-state");
  node.append(el("span", "empty-mark", mark), el("h2", "", title), el("p", "", body));
  return node;
}

function compareValue(left, right, direction) {
  const a = left === null || left === undefined ? -Infinity : left;
  const b = right === null || right === undefined ? -Infinity : right;
  let result;
  if (typeof a === "string" || typeof b === "string") result = String(a).localeCompare(String(b));
  else result = a - b;
  return direction === "asc" ? result : -result;
}

function sortedTeams(teams, sort) {
  return [...teams].sort((a, b) => (
    compareValue(a[sort.key], b[sort.key], sort.direction)
    || a.teamName.localeCompare(b.teamName)
  ));
}

function sortButton(label, key, state, onChange) {
  const button = el("button", "sort-button");
  button.type = "button";
  button.dataset.active = String(state.key === key);
  const direction = state.key === key ? state.direction : null;
  button.setAttribute("aria-label", `Sort by ${label}${direction ? `, currently ${direction}ending` : ""}`);
  button.append(el("span", "", label));
  if (direction) button.append(el("span", "", direction === "asc" ? "↑" : "↓"));
  button.addEventListener("click", () => {
    const nextDirection = state.key === key && state.direction === "desc" ? "asc" : "desc";
    onChange({ key, direction: nextDirection });
  });
  return button;
}

function renderHeader(stats, context) {
  const { metadata } = stats;
  byId("brandName").textContent = metadata.name;
  byId("brandSeason").textContent = `Sleeper · ${metadata.season}`;
  const completed = metadata.completedWeekCount;

  if (stats.liveWeek) {
    byId("statusTitle").textContent = `Week ${stats.liveWeek.week} live`;
    byId("statusDetail").textContent = `${completed} finalized week${completed === 1 ? "" : "s"} included`;
  } else if (!completed) {
    byId("statusTitle").textContent = `Waiting for Week ${metadata.startWeek}`;
    byId("statusDetail").textContent = metadata.seasonStartDate
      ? `Regular season starts ${new Date(`${metadata.seasonStartDate}T00:00:00`).toLocaleDateString(undefined, { month: "long", day: "numeric" })}`
      : "No finalized scores yet";
  } else if (metadata.lastCompletedWeek >= metadata.regularSeasonEnd) {
    byId("statusTitle").textContent = "Regular season complete";
    byId("statusDetail").textContent = `${completed} finalized weeks included`;
  } else {
    byId("statusTitle").textContent = `Through Week ${metadata.lastCompletedWeek}`;
    byId("statusDetail").textContent = `${completed} finalized week${completed === 1 ? "" : "s"} included`;
  }

  byId("dataStatus").classList.toggle("is-stale", Boolean(context.isStale));
}

function renderNotice(stats, context) {
  const region = byId("noticeRegion");
  region.replaceChildren();
  const messages = [];
  if (context.isStale) messages.push(`Showing saved data from ${dateTime(context.cachedAt)}.`);
  else if (context.fromCache) messages.push(`Showing a recent local update from ${dateTime(context.cachedAt)}.`);
  messages.push(...(context.warnings || stats.warnings));
  if (!messages.length) return;

  const notice = el("div", "notice");
  const copy = el("div");
  copy.append(el("strong", "", context.isStale ? "Saved data" : "Heads up"), document.createTextNode(messages.join(" ")));
  const close = el("button", "", "Dismiss");
  close.type = "button";
  close.addEventListener("click", () => notice.remove());
  notice.append(copy, close);
  region.append(notice);
}

function colorForTeam(team, teams) {
  const index = teams.findIndex((entry) => entry.rosterId === team.rosterId);
  return COLORS[(index < 0 ? team.rosterId : index) % COLORS.length];
}

function renderLiveWeek(stats, context) {
  const root = byId("liveWeekRegion");
  root.replaceChildren();
  const current = context.currentWeekMatchups;
  if (!current || current.status === "unavailable") {
    root.append(emptyState("Matchups unavailable", "The current week's schedule is not available yet. Use Refresh to check again."));
    return;
  }
  if (current.status === "season_complete") {
    root.append(emptyState("Regular season complete", "Live matchup coverage ends after Week 14. Finalized statistics remain in the Scoreboard and Stat book."));
    return;
  }
  const statusLabel = { awaiting_scoring: "Awaiting scoring", provisional: "Provisional", final: "Final" }[current.status];
  const panel = el("section", "panel section-panel");
  const header = el("div", "section-heading compact");
  header.append(el("h2", "", `Week ${current.week} matchups`));
  if (current.status !== "provisional") header.append(el("span", "live-badge", statusLabel));
  const note = el("p", "section-note matchup-updated", `Updated ${dateTime(context.cachedAt)} · Use Refresh for new scores.`);
  const cards = el("div", "matchup-grid");
  current.matchups.forEach((matchup) => {
    const card = el("article", "matchup-card");
    card.setAttribute("aria-label", `${matchup.teams[0].teamName} versus ${matchup.teams[1].teamName}`);
    matchup.teams.forEach((team) => {
      const row = el("div", `matchup-side${team.rosterId === matchup.leaderRosterId ? " is-leading" : ""}`);
      row.append(makeTeamLabel(team), el("strong", "matchup-score", point(team.score)));
      card.append(row);
    });
    const leader = matchup.teams.find((team) => team.rosterId === matchup.leaderRosterId);
    const label = current.status === "awaiting_scoring" ? "Awaiting scoring"
      : leader ? `${leader.teamName} ${current.status === "final" ? "won" : "leads"} by ${point(matchup.margin)}` : "Tied";
    card.append(el("p", "matchup-margin", label));
    cards.append(card);
  });
  panel.append(header, note, cards);
  if (current.unpairedTeams.length) panel.append(el("p", "section-note", `Opponent unavailable: ${current.unpairedTeams.map((team) => team.teamName).join(", ")}.`));
  root.append(panel);
}

function renderTrendControls(stats) {
  const root = byId("trendControls");
  root.replaceChildren();
  if (!stats.weeks.length) return;
  const validRosters = new Set(stats.teams.map((team) => team.rosterId));
  [...uiState.selectedRosters].forEach((rosterId) => {
    if (!validRosters.has(rosterId)) uiState.selectedRosters.delete(rosterId);
  });
  if (!uiState.selectedRosters.size) {
    stats.teams.slice(0, 3).forEach((team) => uiState.selectedRosters.add(team.rosterId));
  }
  stats.teams.forEach((team) => {
    const label = el("label");
    label.style.setProperty("--team-color", colorForTeam(team, stats.teams));
    const input = el("input");
    input.type = "checkbox";
    input.checked = uiState.selectedRosters.has(team.rosterId);
    input.disabled = !input.checked && uiState.selectedRosters.size >= 4;
    input.addEventListener("change", () => {
      if (input.checked) uiState.selectedRosters.add(team.rosterId);
      else if (uiState.selectedRosters.size > 1) uiState.selectedRosters.delete(team.rosterId);
      renderTrend(stats);
    });
    label.append(input, document.createTextNode(team.teamName));
    root.append(label);
  });
}

function renderTrendChart(stats) {
  const root = byId("trendChart");
  root.replaceChildren();
  if (!stats.weeks.length) {
    root.append(emptyState("No finalized trends yet", "The chart will begin with the first finalized regular-season week.", "01"));
    return;
  }

  const selected = stats.teams.filter((team) => uiState.selectedRosters.has(team.rosterId));
  const values = stats.weeks.flatMap((week) => [
    week.leagueMedian,
    ...selected.map((team) => week.entries.find((entry) => entry.rosterId === team.rosterId)?.score).filter(Number.isFinite),
  ]);
  let yMin = Math.floor((Math.min(...values) - 10) / 10) * 10;
  let yMax = Math.ceil((Math.max(...values) + 10) / 10) * 10;
  if (yMin === yMax) { yMin -= 10; yMax += 10; }

  const width = 980;
  const height = 360;
  const margin = { top: 20, right: 24, bottom: 46, left: 56 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const x = (index) => margin.left + (stats.weeks.length === 1 ? plotWidth / 2 : (index / (stats.weeks.length - 1)) * plotWidth);
  const y = (value) => margin.top + ((yMax - value) / (yMax - yMin)) * plotHeight;

  const wrap = el("div", "chart-wrap");
  const svg = svgEl("svg", {
    class: "trend-svg",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-labelledby": "trendSvgTitle trendSvgDesc",
  });
  svg.append(svgEl("title", { id: "trendSvgTitle" }), svgEl("desc", { id: "trendSvgDesc" }));
  svg.querySelector("title").textContent = "Selected team scores by finalized week";
  svg.querySelector("desc").textContent = "Solid lines show selected teams. The dashed line shows each week's league median.";

  for (let step = 0; step <= 4; step += 1) {
    const value = yMin + ((yMax - yMin) * step / 4);
    const yPosition = y(value);
    svg.append(svgEl("line", { class: "chart-grid", x1: margin.left, x2: width - margin.right, y1: yPosition, y2: yPosition }));
    const label = svgEl("text", { class: "chart-axis-label", x: margin.left - 10, y: yPosition + 4, "text-anchor": "end" });
    label.textContent = Math.round(value);
    svg.append(label);
  }
  stats.weeks.forEach((week, index) => {
    const label = svgEl("text", { class: "chart-axis-label", x: x(index), y: height - 15, "text-anchor": "middle" });
    label.textContent = `W${week.week}`;
    svg.append(label);
  });

  const medianPath = stats.weeks.map((week, index) => `${index ? "L" : "M"}${x(index)},${y(week.leagueMedian)}`).join(" ");
  svg.append(svgEl("path", { class: "chart-median", d: medianPath }));

  selected.forEach((team) => {
    const color = colorForTeam(team, stats.teams);
    const points = stats.weeks.map((week, index) => ({
      week: week.week,
      score: week.entries.find((entry) => entry.rosterId === team.rosterId)?.score,
      x: x(index),
    })).filter((entry) => Number.isFinite(entry.score));
    const path = points.map((entry, index) => `${index ? "L" : "M"}${entry.x},${y(entry.score)}`).join(" ");
    svg.append(svgEl("path", { class: "chart-line", d: path, stroke: color }));
    points.forEach((entry) => {
      const dot = svgEl("circle", { class: "chart-dot", cx: entry.x, cy: y(entry.score), r: 5, fill: color, tabindex: 0 });
      const title = svgEl("title");
      title.textContent = `${team.teamName}, Week ${entry.week}: ${point(entry.score)}`;
      dot.append(title);
      svg.append(dot);
    });
  });
  wrap.append(svg, el("p", "chart-legend-note", "Dashed line: weekly league median · Choose up to four teams"));
  root.append(wrap);
}

function renderTrend(stats) {
  renderTrendControls(stats);
  renderTrendChart(stats);
}

function heatClass(mode, entry, teamCount) {
  if (!entry) return "";
  if (mode === "rank") {
    if (entry.rank <= 3) return "heat-great";
    if (entry.rank >= teamCount - 2) return "heat-bad";
    return "heat-average";
  }
  return `heat-${entry.quality}`;
}

function renderMatrixControls(stats) {
  const root = byId("matrixControls");
  root.replaceChildren();
  [
    ["score", "Scores"],
    ["delta", "Vs median"],
    ["rank", "Rank"],
  ].forEach(([mode, label]) => {
    const button = el("button", "segment-button", label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(uiState.matrixMode === mode));
    button.addEventListener("click", () => {
      uiState.matrixMode = mode;
      renderMatrix(stats);
    });
    root.append(button);
  });
}

function matrixValue(mode, entry) {
  if (!entry) return "—";
  if (mode === "delta") return signed(entry.deltaMedian);
  if (mode === "rank") return integer(entry.rank);
  return point(entry.score);
}

function renderMatrixTable(stats) {
  const root = byId("matrixTable");
  root.replaceChildren();
  const weekNumbers = [];
  for (let week = stats.metadata.startWeek; week <= stats.metadata.regularSeasonEnd; week += 1) {
    weekNumbers.push(week);
  }
  const weeksByNumber = new Map(stats.weeks.map((week) => [week.week, week]));
  if (stats.liveWeek) weeksByNumber.set(stats.liveWeek.week, stats.liveWeek);
  const table = el("table", "data-table matrix-table");
  table.append(el("caption", "visually-hidden", `${uiState.matrixMode} by team for every regular-season week`));
  const thead = el("thead");
  const head = el("tr");
  const teamHead = el("th", "", "Team");
  teamHead.scope = "col";
  head.append(teamHead);
  weekNumbers.forEach((weekNumber) => {
    const week = weeksByNumber.get(weekNumber);
    const th = el("th", week?.status === "live" ? "matrix-live-head" : week ? "" : "matrix-future", `W${weekNumber}`);
    th.scope = "col";
    if (week?.status === "live") {
      th.append(el("span", "matrix-live-label", "Live"));
      th.title = "Provisional scores; excluded from season totals";
    }
    head.append(th);
  });
  const summaryLabel = uiState.matrixMode === "score" ? "Average" : uiState.matrixMode === "delta" ? "Avg ±" : "Avg rank";
  const summaryHead = el("th", "", summaryLabel);
  summaryHead.scope = "col";
  head.append(summaryHead);
  thead.append(head);

  const tbody = el("tbody");
  sortedTeams(stats.teams, { key: "total", direction: "desc" }).forEach((team) => {
    const row = el("tr");
    const identity = el("td");
    identity.append(makeTeamLabel(team));
    row.append(identity);
    weekNumbers.forEach((weekNumber) => {
      const week = weeksByNumber.get(weekNumber);
      const entry = week?.entries.find((candidate) => candidate.rosterId === team.rosterId);
      const classNames = [
        heatClass(uiState.matrixMode, entry, stats.metadata.teamCount),
        week?.status === "live" ? "matrix-live-cell" : "",
        week ? "" : "matrix-future",
      ].filter(Boolean).join(" ");
      const cell = el("td", classNames, matrixValue(uiState.matrixMode, entry));
      if (week?.status === "live") cell.title = "Provisional; excluded from aggregates";
      row.append(cell);
    });
    const summary = uiState.matrixMode === "score"
      ? point(team.average)
      : uiState.matrixMode === "delta"
        ? signed(team.averageDeltaMedian)
        : rank(team.averageRank);
    row.append(el("td", "", summary));
    tbody.append(row);
  });

  const tfoot = el("tfoot");
  if (uiState.matrixMode !== "rank") {
    const rows = uiState.matrixMode === "score"
      ? [
          ["League average", (week) => point(week.leagueAverage), point(stats.league.weeklyAverageMean)],
          ["League median", (week) => point(week.leagueMedian), point(stats.league.medianOfWeeklyMedians)],
        ]
      : [
          ["Mean delta", (week) => signed((week.entries.reduce((total, entry) => total + entry.deltaMedian, 0) / week.entries.length)), signed(stats.league.meanDeltaMedian)],
          ["Median delta", () => point(0), signed(stats.league.medianDeltaMedian)],
        ];
    rows.forEach(([label, formatter, summary]) => {
      const row = el("tr");
      const th = el("th", "", label);
      th.scope = "row";
      row.append(th);
      weekNumbers.forEach((weekNumber) => {
        const week = weeksByNumber.get(weekNumber);
        const cell = el("td", week?.status === "live" ? "matrix-live-cell" : week ? "" : "matrix-future", week ? formatter(week) : "—");
        row.append(cell);
      });
      row.append(el("td", "", summary));
      tfoot.append(row);
    });
  }
  table.append(thead, tbody, tfoot);
  const legend = el("div", "matrix-legend");
  if (uiState.matrixMode === "rank") {
    legend.append(
      el("span", "legend-great", "Top 3"),
      el("span", "legend-average", "Middle"),
      el("span", "legend-bad", "Bottom 3"),
    );
  } else {
    legend.append(
      el("span", "legend-great", `>${stats.metadata.greatWeekDelta} above median`),
      el("span", "legend-average", `Within ±${stats.metadata.greatWeekDelta}`),
      el("span", "legend-bad", `>${stats.metadata.greatWeekDelta} below median`),
    );
  }
  legend.append(el("small", "", "Season averages use finalized weeks only."));
  root.append(table, legend);
}

function renderMatrix(stats) {
  renderMatrixControls(stats);
  renderMatrixTable(stats);
}

function renderWeekly(stats) {
  renderTrend(stats);
}

function renderBenchmarks(stats) {
  const root = byId("leagueBenchmarks");
  root.replaceChildren();
  const metrics = [
    ["Total mean", point(stats.league.overallMean)],
    ["Total median", point(stats.league.overallMedian)],
    ["Total deviation", point(stats.league.scoreDeviation)],
    ["Median weekly median", point(stats.league.medianOfWeeklyMedians)],
    ["Mean pts > median", signed(stats.league.meanDeltaMedian)],
    ["Median pts > median", signed(stats.league.medianDeltaMedian)],
    ["Mean pts deviation", point(stats.league.deltaDeviation)],
    ["Finalized scores", integer(stats.league.scoreCount)],
  ];
  metrics.forEach(([label, value]) => {
    const card = el("article", "benchmark");
    card.append(el("span", "", label), el("strong", "", value));
    root.append(card);
  });
}

function statbookColumns() {
  return [
    { group: "Scoring", label: "Games", key: "games", format: integer },
    { group: "Scoring", label: "Total", key: "total", format: point },
    { group: "Scoring", label: "Average", key: "average", format: point },
    { group: "Scoring", label: ">120", key: "over120", format: integer },
    { group: "Scoring", label: "<110", key: "under110", format: integer },
    { group: "Scoring", label: "Best", key: "bestScore", format: point },
    { group: "Scoring", label: "Worst", key: "worstScore", format: point },
    { group: "Scoring", label: "Range", key: "scoreRange", format: point },
    { group: "Scoring", label: "Score SD", key: "scoreDeviation", format: point },
    { group: "Median", label: "Record", key: "medianWinPct", format: (_, team) => team.medianRecord },
    { group: "Median", label: "Win %", key: "medianWinPct", format: percentage },
    { group: "Median", label: "Avg ±", key: "averageDeltaMedian", format: signed },
    { group: "Median", label: "Great", key: "greatWeeks", format: integer },
    { group: "Median", label: "Average", key: "averageWeeks", format: integer },
    { group: "Median", label: "Bad", key: "badWeeks", format: integer },
    { group: "Rank", label: "Average", key: "averageRank", format: rank },
    { group: "Rank", label: "SD", key: "rankDeviation", format: rank },
    { group: "Rank", label: "Best", key: "bestRank", format: integer },
    { group: "Rank", label: "Worst", key: "worstRank", format: integer },
  ];
}

function renderStatbookTable(stats) {
  const root = byId("statbookTable");
  root.replaceChildren();
  const columns = statbookColumns();
  const table = el("table", "data-table statbook-table");
  table.append(el("caption", "visually-hidden", "Complete True League scoring statistics"));
  const thead = el("thead");
  const groupRow = el("tr");
  const teamHead = el("th", "group-head", "Team");
  teamHead.scope = "col";
  teamHead.rowSpan = 2;
  groupRow.append(teamHead);
  ["Scoring", "Median", "Rank"].forEach((group) => {
    const th = el("th", "group-head", group);
    th.scope = "colgroup";
    th.colSpan = columns.filter((column) => column.group === group).length;
    groupRow.append(th);
  });
  const labelRow = el("tr");
  columns.forEach((column) => {
    const th = el("th");
    th.scope = "col";
    th.append(sortButton(column.label, column.key, uiState.statSort, (sort) => {
      uiState.statSort = sort;
      renderStatbookTable(stats);
    }));
    labelRow.append(th);
  });
  thead.append(groupRow, labelRow);
  const tbody = el("tbody");
  sortedTeams(stats.teams, uiState.statSort).forEach((team) => {
    const row = el("tr");
    const identity = el("td");
    identity.append(makeTeamLabel(team));
    row.append(identity);
    columns.forEach((column) => {
      const cell = el("td");
      const rendered = column.format(team[column.key], team);
      if (column.key === "averageDeltaMedian") cell.className = team[column.key] > 0 ? "positive" : team[column.key] < 0 ? "negative" : "";
      if (column.label === "Record") cell.append(el("span", "record-pill", rendered));
      else cell.textContent = rendered;
      row.append(cell);
    });
    tbody.append(row);
  });
  table.append(thead, tbody);
  root.append(table);
}

function renderGlossary(stats) {
  const root = byId("glossaryGrid");
  root.replaceChildren();
  const threshold = stats.metadata.greatWeekDelta;
  const items = [
    ["Weekly average", "The arithmetic mean of all finalized team scores for that week."],
    ["Weekly median", "The midpoint of all finalized team scores; with 10 teams, the middle two scores are averaged."],
    ["Points above median", "A team's score minus that week's league median. Negative values are below the median."],
    ["Median record", "A win for scoring above the weekly median, a loss below it, and a tie at exactly the median."],
    ["Median win %", "Median wins plus half of median ties, divided by finalized weeks."],
    ["Average PRK", "Average weekly points rank. Tied scores share a competition rank."],
    [">120 / <110", "Counts use strict boundaries: scores must be greater than 120 or less than 110."],
    ["Range", "Best weekly score minus worst weekly score."],
    ["Score deviation", "Sample standard deviation of a team's weekly scores; shown after at least two finalized weeks."],
    ["Great week", `More than ${threshold} points above that week's median.`],
    ["Average week", `Between −${threshold} and +${threshold} points from the weekly median, inclusive.`],
    ["Bad week", `More than ${threshold} points below that week's median.`],
    ["Rank deviation", "Sample standard deviation of weekly points rank; lower means steadier placement."],
    ["Total mean / median", "Mean and median across every finalized team score in the regular season."],
    ["Median weekly median", "The median of the weekly league-median values."],
    ["Mean pts > median", "The average of every team-week score-minus-median value."],
    ["Mean pts deviation", "Population standard deviation of all score-minus-weekly-median values."],
    ["Finalized only", "Live scores are visible but never affect aggregates until Sleeper finalizes the week."],
  ];
  items.forEach(([term, definition]) => {
    const wrapper = el("dl", "glossary-item");
    wrapper.append(el("dt", "", term), el("dd", "", definition));
    root.append(wrapper);
  });
}

function renderStatbook(stats) {
  renderBenchmarks(stats);
  renderStatbookTable(stats);
  renderGlossary(stats);
}

export function initializeUi({ onRefresh }) {
  refreshHandler = onRefresh;
  byId("refreshButton").addEventListener("click", refreshHandler);
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  tabs.forEach((tab, index) => {
    tab.tabIndex = tab.getAttribute("aria-selected") === "true" ? 0 : -1;
    tab.addEventListener("click", () => activateTab(tab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      let next = index;
      if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      tabs[next].focus();
      activateTab(tabs[next]);
    });
  });
}

function activateTab(activeTab) {
  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    const active = tab === activeTab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    byId(tab.dataset.view).hidden = !active;
  });
  if (activeTab.id === "liveTab") refreshHandler();
}

export function setRefreshState(isLoading) {
  const button = byId("refreshButton");
  button.disabled = isLoading;
  button.setAttribute("aria-busy", String(isLoading));
  button.replaceChildren(el("span", "", "↻"), document.createTextNode(isLoading ? " Checking Sleeper…" : " Refresh data"));
}

export function renderLoading() {
  byId("liveWeekRegion").replaceChildren(el("p", "section-note", "Loading matchups…"));
  byId("matrixControls").replaceChildren();
  byId("matrixTable").replaceChildren(emptyState("Loading weekly scores", "Connecting to Sleeper.", "↻"));
}

export function renderError(error) {
  byId("liveWeekRegion").replaceChildren(emptyState("Matchups unavailable", "Use Refresh to try again."));
  byId("noticeRegion").replaceChildren();
  byId("statusTitle").textContent = "Sleeper unavailable";
  byId("statusDetail").textContent = "No saved league data is available";
  byId("matrixControls").replaceChildren();
  byId("matrixTable").replaceChildren(emptyState("Scores unavailable", error?.message || "Sleeper data could not be loaded.", "!"));
  const panel = el("section", "panel error-panel");
  panel.append(el("h2", "", "Could not load league statistics"), el("p", "", error?.message || "Sleeper data could not be loaded."));
  const retry = el("button", "primary-button", "Try again");
  retry.type = "button";
  retry.addEventListener("click", refreshHandler);
  panel.append(retry);
  byId("matrixTable").replaceChildren(panel);
}

export function renderDashboard(stats, context = {}) {
  renderHeader(stats, context);
  renderNotice(stats, context);
  renderMatrix(stats);
  renderWeekly(stats);
  renderLiveWeek(stats, context);
  renderStatbook(stats);
}

export function renderConnectionWarning() {
  byId("noticeRegion").replaceChildren(el("div", "notice", "Connection interrupted. Displayed scores have been retained; use Refresh to try again."));
  byId("dataStatus").classList.add("is-stale");
}
