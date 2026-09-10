# True League on Sleeper

Node 24 / Express 5 hosts the dashboard and a shared Sleeper snapshot service.
Only this directory is deployed. The ESPN dashboard is independent.

## Local development

Use Node 24 (`nvm install 24 && nvm use 24` if you use nvm), then:

```sh
cd sleeper
npm ci
npm test
npm run dev
```

Open http://localhost:4174. `npm start` runs without the development watcher.
The old Python static server no longer provides the required API.

Environment settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| PORT | 4174 locally; Render supplies its own | HTTP port on 0.0.0.0 |
| LEAGUE_ID | 1395542220504854528 | Numeric string, never converted to Number |
| DATA_DIR | sleeper/.data | Persistent snapshot directory |
| PUBLIC_BASE_URL | RENDER_EXTERNAL_URL, then localhost | Absolute site URL used for social previews |
| NODE_ENV | Set to production on Render | Express production mode |

## Render deployment

Create a Blueprint from this repository. Set the **Blueprint Path** to
`sleeper/render.yaml` (Render normally searches the repository root). The
Blueprint provisions one Starter web service with root directory `sleeper`, a
1 GB disk at `/var/data`, build `npm ci && npm test`, start `npm start`, and
health path `/healthz`. The Node major is pinned by `.node-version` and engines.

Alternatively create a web service manually using those same values. Set
`DATA_DIR=/var/data/sleeper`, `NODE_ENV=production`, and the league ID above.
Leave PUBLIC_BASE_URL unset for the generated Render URL; set it to your HTTPS
origin when adding a custom domain. No Sleeper credentials are needed.

The disk makes this a single-instance service and causes a short interruption
during deployments. Scheduling runs in-process; no separate cron service is
needed. Persistent storage is accessed only at runtime, never during builds.

First deployment acceptance:

1. Visit the public HTTPS URL and verify all tabs and assets load.
2. Open Live Scores and confirm five matchup cards when the schedule is available.
3. Check `/healthz`: storage is healthy and dataAvailable becomes true.
4. Restart the service; confirm the successful snapshot remains readable.
5. Confirm logs show successful refreshes and sensible timestamps. Upstream outage
   recovery is covered locally by injected-failure HTTP tests; a production outage
   should return saved scores with a warning, not an empty dashboard.

## Data flow and API

Startup and a 15-minute timer fetch the full regular season. Browser load,
opening Live Scores, and Refresh can also request a refresh. All callers share
one in-flight fetch and a 60-second cooldown, even after failures. There is no
browser polling. Sleeper's own update cadence limits freshness.

GET `/api/dashboard` reads saved data (initializing if necessary).
POST `/api/refresh` refreshes subject to the shared cooldown. Both return:
`{schemaVersion:1, stats, currentWeekMatchups, lastSuccessfulFetchAt, stale, warnings}`.
Responses are not cached by the browser. Saved data returns 200 even during an
outage; no usable snapshot returns 503. Data is stale after a failed refresh or
20 minutes without success. `/healthz` checks storage separately from upstream
data availability, so Sleeper outages do not cause restart loops.

Only finalized Weeks 1–14 enter statistics. All-zero schedules are preserved for
matchup display, but never imply completion. Commissioner overrides, including
zero, take precedence. Invalid/incomplete refreshes retain the previous snapshot.
Live Scores stops at the end of the regular season. No player breakdowns or
projections are fetched. Team names, managers, and scores are publicly visible.

## Persistence, export, and restore

Each league has `DATA_DIR/<leagueId>/latest.json` and `revisions/`. Snapshots
include a schema version and content checksum. Writes use temporary files and
atomic rename. Changed normalized data creates an archive; successful checks
with identical data update the latest timestamp without creating a revision.
Revisions are not pruned automatically. Monitor disk usage in Render.

Startup checks latest.json and then tries archives newest-first. A corrupt file
is logged and retained for inspection. Fetch/persistence failures never replace
the in-memory last successful snapshot. A failed disk health check returns 503.

For an export, use Render's SSH/SCP access to copy the entire league directory
to a dated local backup. Copy archives first and latest.json last; each file is
published atomically. Store the export outside the service. Render also takes
daily disk snapshots; a disk restore rolls back the entire disk.

For restoration, stop application writes using Render's maintenance workflow,
restore the exported league directory to the same DATA_DIR, and restart. Do not
edit individual scores in snapshot JSON: checksums will reject edited snapshots.
To recover from latest.json corruption alone, restart and let archive fallback
recover automatically. Preserve the original files until recovery is verified.

References: [Render disks](https://render.com/docs/disks),
[monorepo deployment](https://render.com/docs/monorepo-support),
[Sleeper API](https://docs.sleeper.com/).
