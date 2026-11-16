# GateAhead API

India-wide railway level-crossing alerts. This repository is the Python service: FastAPI and the Telegram bot. The website and the phone app are separate repositories.

Crossings and trains are fetched from the user's location. There is no hardcoded station, train, or gate catalog.

## What users do

1. `/setlocation` — Telegram GPS (where they are).
2. `/nearby` — OpenStreetMap crossings around them.
3. `/predict` — trains on that stretch (erail train list + RailYatri live status).
4. `/subscribe` — ping when a nearby cluster is about to close.
5. `/markgate` — GPS at a missing barrier. Rejected if not on a railway track.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # set TELEGRAM_BOT_TOKEN
pytest
python -m gateahead.bot
```

HTTP API (same engine):

```bash
uvicorn gateahead.api.app:app --host 0.0.0.0 --port 8000
```

The website calls this API under `/api`.

## Shared snapshots and community reports

Live train responses are cached for 90 seconds in SQLite, preserving their fetch time so report age continues increasing between checks. API and bot processes using the same database share snapshots and fetch leases, including concurrent checks. Separate hosts need a shared storage design before they can share this cache. The cache reduces provider traffic; it does not improve source freshness or establish an upstream request allowance.

`GET /predict` includes `generated_at`, `refresh_after_seconds`, and `gate_predictions`. Each gate prediction has its own `id`, train, ETA, barrier lead time, status, basis, and report age. Grouped approaching rows list only gates still ahead; clients must not apply a cluster's minimum ETA to every gate. Approaching trains include `basis` and `age_min`; scene trains also include their estimation basis and `position_on_segment`. That flag is false when a distant position would otherwise be clamped to the end of the short displayed track. Clients use it to avoid placing a faraway train beside the gate. Scene trains include `snapshot_at` (Unix seconds) and optional `motion` (`direction` on the rail, `speed_kmh`, `valid_for_sec`). For a closed crossing, the drawn track expands to include the estimated approaching position. The motion validity window ends when the source report reaches eight minutes old. Clients use the crossing ETA to estimate movement in real time, bounded by that validity window; they must not replay a fixed-duration crossing when a scene opens. Stopped trains, reports at least eight minutes old, uncertain timings, and unknown travel directions omit `motion`. Later timetable estimates remain available in crossing details. Clients keep prediction labels separate from actual barrier observations.

Rail fragments use the closest forward connection and reject turnbacks or lateral jumps onto neighbouring parallel tracks. A train keeps its identity and travel direction across crossing views. Track placement is estimated because the source does not supply confirmed occupied-track assignments.

Train discovery combines station-pair searches with at most three departure boards: a local station and regional anchors on either side of the area, preferring junctions and central stations. The regional catalog is searched within 150 km without expanding the crossing map or adding Overpass requests. A geographic alignment check keeps the fallback board near the regional axis rather than selecting a distant inland branch. These lookups are cached for ten minutes. Every geographically matching indexed route can supplement provider discovery, including expresses skipping local stations. The old 30-candidate, five-fallback, and 200-route cutoffs have been removed. An SQLite R-tree indexes the legs between located timetable points; existing routes are indexed during migration without changing their freshness. The coarse lookup is padded by 25 km for bends and is followed by passage and crossing placement checks. This index describes timetable legs, not confirmed occupied tracks or a complete national railway graph.

`LIVE_CHECK_BUDGET` (default 30) bounds uncached live train checks per request. Fresh shared snapshots add no provider calls. Pending candidates rotate using their last check time, with scheduled passage proximity breaking ties. Both `/predict` and `/scene` return `coverage`, including `candidates`, `live_candidates`, `live_checked`, `live_reports`, `unavailable_live`, `unavailable_discovery`, `pending_live`, `missing_or_stale_routes`, and `limited`. `unavailable_discovery` counts failed or unreadable station-board/pair lookups for that request; successfully indexed routes and other discoveries remain available. A provider's identifiable delimiter-only empty pair response remains a valid empty result, while unreadable pages do not establish an empty line or become cached empty boards. The clients show incomplete checks instead of treating a limited result as an empty line. `checked` counts live lookups, including fresh cached lookups. Coverage state belongs to the request and is not shared between simultaneous locations. Timetable eligibility retains the existing live window (six hours behind to 150 minutes ahead) and the eight-hour schedule display window. Unknown extreme delays and multiple simultaneous runs remain upstream/operating-date limitations.

Malformed map responses and nonfinite map geometry fail over to the next map provider instead of becoming a successful empty map. A valid empty map remains valid. Expected network, timeout, or decoding failures in a train lookup do not discard healthy services from the same request; failed timetable retrievals retain their refresh backoff. Request cancellation still propagates, and a canceled caller cannot cancel another caller's shared live fetch.

Map mirrors share a 75-second lookup budget, with each attempt limited to at most 30 seconds or the remaining budget. This leaves room within the browser's 90-second request window instead of stacking three full mirror timeouts. A stale cached map remains available if all attempts fail; cancellation stops the lookup rather than continuing through the mirrors.

Routes older than `ROUTE_MAX_AGE_HOURS` (default 168) remain discovery hints but cannot provide timetable estimates until refreshed. Failed refreshes retain their previous data and back off. Gate ETAs use route position, direction, report age, and timetable interpolation; the gate's nearest station need not be a scheduled stop. A current moving report takes precedence over schedule eligibility. Operating weekdays refer to the origin departure date, including journeys lasting three or more days. Closure remains an estimate based on the configured lead time, not a barrier observation.

Railway scheduling, discovery priority, live-position timestamps, and generated prediction times explicitly use Indian Standard Time (UTC+05:30), independent of the server's `TZ` setting. Scene snapshot timestamps remain absolute Unix seconds. Deploying on a UTC host therefore does not shift an Indian timetable by five and a half hours or change the origin operating weekday after Indian midnight. Explicit naive datetimes supplied to domain helpers still represent Indian timetable wall time; ordinary API callers do not supply those clocks.

For long nonstop timetable legs whose junctions lie outside the local map, a bend can support an estimated crossing position when the mapped rail continues through the gate in the leg's direction and the trip via the gate fits the published distance. This fallback stays within eight kilometres of the timetable chord and preserves the route bracket. A reported train on a separate mapped rail component excludes that crossing. Other inferred bend estimates retain uncertain timing: they cannot assert closure or animate a crossing, because route geometry alone does not identify the occupied line.

Crossing scenes retain every identified service and expand the view for all trains in the estimated closure window. A scene train may include `track` with `estimated: true`, its own `rail`, `along_km`, `position_on_segment`, and `motion`. That path comes from the nearby connected rail only when it reaches the crossing corridor. The original top-level geometry and per-train fields remain available to existing clients. These projections add no provider requests.

Train names from existing discovery and live payloads are retained in SQLite and applied to timetable-only positions, without additional provider requests. A live name takes precedence; when no official name is available, the route endpoints provide a description instead.

`GET /reports?crossing_id=...` returns recent, unverified community observations. `POST /reports` accepts `crossing_id`, `installation_id`, `state` (`open`, `closed`, or `wrong_location`), `lat`, and `lon`. Submissions are checked against the mapped crossing, replace that installation's previous observation for the same gate, and expire after ten minutes. Installation identifiers are hashed and excluded from responses. Reports do not alter predictions or train alerts.

Predictions and scenes retain their shared limit of 20 requests per IP per ten minutes and now return `Retry-After` when limited. Report submissions have a separate limit of ten per IP per ten minutes. Listing reports does not fetch train data.

## Refreshable train catalog

Run the catalog worker alongside the API or bot, using the same `GATEAHEAD_DATA` directory. It expands the known train list through station boards and fetches current timetable pages independently of user searches. It scans every station/halt in the installed catalog over successive bounded batches. It does not import historical train schedules or require a new API key. Initial bootstrap may take many batches; a successful batch is not proof of nationwide coverage.

```bash
python -m gateahead.catalog status
python -m gateahead.catalog sync --station-limit 25 --route-limit 100
python -m gateahead.catalog sync --watch --interval-seconds 60
python -m gateahead.catalog audit --output /tmp/gateahead-station-coverage.csv
```

The worker refreshes timetables older than 24 hours and boards older than seven days. `--route-refresh-hours`, `--station-refresh-hours`, and batch limits control this work; zero refresh hours forces a recheck of eligible entries. Requests are serial and spaced at least two seconds apart by default. Adjust volume to the provider allowance; pacing does not establish permission or an allowance. Empty boards and malformed/empty timetables count as failures, retain old data, and receive exponential backoff from five minutes up to six hours. Three consecutive failures stop that phase of the batch. Progress, backoff, and a renewable worker lease live in SQLite so restarts resume and concurrent workers do not duplicate a batch. Separate hosts require shared storage or a single designated worker.

Use `--trains /path/train-numbers.txt` to seed five-digit numbers, or `--stations /path/stations.json` to import a station GeoJSON/JSON catalog with the existing station parser. Without an installed station catalog, sync attempts the existing station bootstrap. Station coordinates are resolved into timetables before indexing. A newer station source can therefore expand discovery without code changes.

`GET /coverage` reads only the database. It reports known trains, fresh/stale/missing routes, routes due for daily refresh, failed refreshes, scan counts, route geometry gaps, and oldest/newest successful route check times. Check times describe provider retrieval, not a verified timetable edition. The CSV audit includes every installed station and its route/board coverage; route counts are based on explicit timetable points and do not count inferred passage between distant stops. Neither output independently verifies the completeness of the upstream service list, OSM crossings, freight coverage, diversions, or physical barriers. `national_coverage_verified` remains false.

## Gate timing

`/predict` and `/scene` give two numbers for each train at a crossing:

- `eta` / `eta_min`: minutes until the train reaches the crossing.
- `closes_in` / `closes_in_min`: minutes until the barrier comes down. This is `eta` minus `GATE_LEAD_MIN` (default 5). Zero or less predicts that the barrier is down; this is not a physical barrier observation.

A train stays `closed` until 2 minutes after its estimated arrival, because the barrier stays down while it clears the road. After that it moves to the `passed` list for 5 minutes, where `ago` / `ago_min` says how many minutes ago it went over.

A stopped report is held at its reported station; its age is never treated as travel time. Its estimated arrival cannot become earlier than the remaining run from that station. If the report is at least eight minutes old and the crossing is still ahead, `/predict` and `/scene` put the train in `awaiting_update` with its last reported station, age, and affected crossing IDs. It receives no passing or closure countdown and is not moved to `passed` by the timer. Clients show “Position update needed” and label other services as other reported trains. A newer moving report restores an estimate; a reported position beyond the gate can establish estimated passage. Stopped scene/list estimates also stop counting down in the browser and become unresolved at the same eight-minute age limit. Arrivals and departures at intermediate timetable stops use their respective clocks.

The development-only minute-by-minute simulator lives in [`simulator/`](simulator/README.md). It is excluded from the application package. Running `pytest` from this repository runs both backend and simulator tests. The combined workspace audit and scenario inventory are in [`docs/qa/`](docs/qa/TEST_REPORT.txt); those reports also describe the separately versioned website and Flutter checks.

## Layout

```
src/gateahead/     domain + Telegram + FastAPI
  osm.py           Overpass (crossings, stations, rails)
  verify.py        user pin checks
  erail.py         trains between two station codes
  live.py          RailYatri live position
  catalog.py       resumable station discovery, daily timetable refresh, audits
  store.py         SQLite users, gates, train catalog, spatial route index
docs/IMPLEMENTATION_PLAN.md
```
