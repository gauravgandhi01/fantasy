import { initializeUi, renderDashboard, renderError, renderLoading, setRefreshState, renderConnectionWarning } from "./ui.js";

let pending = false;
let hasData = false;

async function requestDashboard(refresh = false) {
  if (pending) return;
  pending = true;
  setRefreshState(true);
  try {
    const response = await fetch(refresh ? "/api/refresh" : "/api/dashboard", {
      method: refresh ? "POST" : "GET", cache: "no-store", signal: AbortSignal.timeout(60000),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load league data.");
    if (payload.schemaVersion !== 1 || !payload.stats) throw new Error("Unexpected dashboard response.");
    renderDashboard(payload.stats, {
      isStale: payload.stale, cachedAt: payload.lastSuccessfulFetchAt,
      warnings: payload.warnings, currentWeekMatchups: payload.currentWeekMatchups,
    });
    hasData = true;
  } catch (error) {
    if (hasData) renderConnectionWarning();
    else renderError(error);
  } finally { pending = false; setRefreshState(false); }
}

initializeUi({ onRefresh: () => requestDashboard(true) });
renderLoading();
await requestDashboard();
await requestDashboard(true);
