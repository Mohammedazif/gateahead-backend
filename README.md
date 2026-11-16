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

Train discovery combines local station-pair searches with departure boards for at most two nearby stations. This includes expresses that stop at only one local station, such as 16512 at Payyanur while skipping Elimala and Trikaripur. Station boards share the ten-minute discovery cache. Up to five nearby, fresh cached timetable candidates can recover services omitted by a provider response, within the existing total cap of 30 live candidates; expired and out-of-area routes are excluded.

Crossing scenes retain every identified service and expand the view for all trains in the estimated closure window. A scene train may include `track` with `estimated: true`, its own `rail`, `along_km`, `position_on_segment`, and `motion`. That path comes from the nearby connected rail only when it reaches the crossing corridor. The original top-level geometry and per-train fields remain available to existing clients. These projections add no provider requests.

Train names from existing discovery and live payloads are retained in SQLite and applied to timetable-only positions, without additional provider requests. A live name takes precedence; when no official name is available, the route endpoints provide a description instead.

`GET /reports?crossing_id=...` returns recent, unverified community observations. `POST /reports` accepts `crossing_id`, `installation_id`, `state` (`open`, `closed`, or `wrong_location`), `lat`, and `lon`. Submissions are checked against the mapped crossing, replace that installation's previous observation for the same gate, and expire after ten minutes. Installation identifiers are hashed and excluded from responses. Reports do not alter predictions or train alerts.

Predictions and scenes retain their shared limit of 20 requests per IP per ten minutes and now return `Retry-After` when limited. Report submissions have a separate limit of ten per IP per ten minutes. Listing reports does not fetch train data.

## Gate timing

`/predict` and `/scene` give two numbers for each train at a crossing:

- `eta` / `eta_min`: minutes until the train reaches the crossing.
- `closes_in` / `closes_in_min`: minutes until the barrier comes down. This is `eta` minus `GATE_LEAD_MIN` (default 5). Zero or less predicts that the barrier is down; this is not a physical barrier observation.

A train stays `closed` until 2 minutes after its estimated arrival, because the barrier stays down while it clears the road. After that it moves to the `passed` list for 5 minutes, where `ago` / `ago_min` says how many minutes ago it went over.

The minute-by-minute simulator that checks these numbers lives outside this repository, in `../simulator`.

## Layout

```
src/gateahead/     domain + Telegram + FastAPI
  osm.py           Overpass (crossings, stations, rails)
  verify.py        user pin checks
  erail.py         trains between two station codes
  live.py          RailYatri live position
  store.py         SQLite users / suggested gates
docs/IMPLEMENTATION_PLAN.md
```
